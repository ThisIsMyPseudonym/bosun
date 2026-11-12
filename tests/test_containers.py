import datetime as dt
import json
from pathlib import Path

import httpx

from bosun.actions import upgrade_argv
from bosun.collectors.containers import (
    distro_of, find_container_files, image_item, parse_compose, parse_dockerfile, parse_image_scan,
    parse_ref, runtime_cycle,
)
from bosun.config import load_settings
from bosun.enrich.eol import EolClient, match_cycle, recommended_cycle
from bosun.lanes import classify
from bosun.models import Source
from bosun.store import Store

NODE = [{"cycle": "24", "eol": "2028-04-30", "lts": True, "latest": "24.9.0"},
        {"cycle": "23", "eol": "2025-06-01", "lts": False, "latest": "23.11.1"},
        {"cycle": "22", "eol": "2027-04-30", "lts": True, "latest": "22.23.4"},
        {"cycle": "18", "eol": "2025-04-30", "lts": True, "latest": "18.20.8"}]
UBUNTU = [{"cycle": "20.04", "codename": "Focal Fossa", "eol": "2025-05-29"}]
DEBIAN = [{"cycle": "12", "codename": "Bookworm", "eol": "2026-06-10", "latest": "12.12"},
          {"cycle": "10", "codename": "Buster", "eol": "2022-09-10", "latest": "10.13"}]
PYTHON = [{"cycle": "3.14", "eol": "2030-10-31"}, {"cycle": "3.11", "eol": "2027-10-31"},
          {"cycle": "3.10", "eol": "2026-10-31"}]


def eol_client(tmp_path, today=dt.date(2026, 10, 23)):
    data = {"nodejs": NODE, "debian": DEBIAN, "python": PYTHON, "ubuntu": UBUNTU}

    def handler(req):
        product = req.url.path.rsplit("/", 1)[-1].removesuffix(".json")
        return httpx.Response(200, json=data[product]) if product in data else httpx.Response(404)

    return EolClient(Store(tmp_path / "db"), httpx.Client(transport=httpx.MockTransport(handler)), today=today)


def test_dockerfile_args_aliases_scratch():
    text = ("ARG BASE_IMAGE=python:3.11-slim\nARG NODE=node:22-alpine\n"
            "FROM ${NODE} AS web\nRUN npm ci \\n  && npm run build\n"
            "FROM --platform=linux/amd64 $BASE_IMAGE AS app\nFROM web\nFROM scratch\nFROM ${UNSET}\n")
    assert parse_dockerfile(text) == ["node:22-alpine", "python:3.11-slim"]


def test_compose_images_and_defaults():
    text = 'services:\n  db:\n    image: "postgres:14-alpine"\n  cache:\n    image: ${REDIS:-redis:7-alpine}\n  x:\n    image: ${NOPE}\n'
    assert parse_compose(text) == ["postgres:14-alpine", "redis:7-alpine"]


def test_ref_parsing():
    r = parse_ref("docker.io/library/node:18-bullseye-slim@sha256:abc")
    assert (r.repo, r.tag, r.floating) == ("node", "18-bullseye-slim", False)
    assert runtime_cycle(r) == "18" and distro_of(r) == ("debian", "bullseye")
    assert runtime_cycle(parse_ref("pgvector/pgvector:pg17")) == "17"
    assert parse_ref("chromadb/chroma").floating and parse_ref("nginx:alpine").floating
    assert distro_of(parse_ref("mcr.microsoft.com/playwright/python:v1.40.0-focal")) == ("ubuntu", "focal")


def test_eol_cycle_matching():
    assert match_cycle(NODE, "22")["cycle"] == "22"
    assert match_cycle(DEBIAN, "buster")["cycle"] == "10"
    assert recommended_cycle(NODE, dt.date(2026, 10, 23)) == "24"


def test_image_item_unsupported_runtime_and_distro(tmp_path):
    eol = eol_client(tmp_path)
    old = image_item(parse_ref("node:18-buster"), Path("C:/r"), Path("C:/r/Dockerfile"), eol)
    assert old.unsupported and old.support_ends == "2022-09-10"  # earliest of runtime / OS
    assert old.available == "node 24 + debian 12" and old.base_os == "debian buster"
    assert image_item(parse_ref("node:22-alpine"), Path("C:/r"), Path("C:/r/Dockerfile"), eol) is None  # healthy
    soon = image_item(parse_ref("python:3.10-slim"), Path("C:/r"), Path("C:/r/Dockerfile"), eol)
    assert soon.ends_soon and not soon.unsupported
    focal = image_item(parse_ref("mcr.microsoft.com/playwright/python:v1.40.0-focal"), Path("C:/r"), Path("C:/r/D"), eol)
    assert focal.unsupported and focal.base_os == "ubuntu focal"
    floating = image_item(parse_ref("redis:latest"), Path("C:/r"), Path("C:/r/D"), eol)
    (c_old, c_soon, c_float) = classify([old, soon, floating], {})
    assert (c_old.lane, c_old.risk) == ("plan", 8.0)  # EOL = high risk, new base image = work
    assert c_soon.lane == "later" and c_float.lane == "batch"


def test_image_scan_parse_and_pull_argv():
    scan = {"results": [{"packages": [{"package": {"name": "openssl"}, "vulnerabilities": [
        {"id": "CVE-1", "severity": [{"type": "CVSS_V3", "score": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"}],
         "affected": [{"ranges": [{"events": [{"introduced": "0"}, {"fixed": "3.0.15"}]}]}]},
        {"id": "CVE-2", "affected": [{"ranges": [{"events": [{"introduced": "0"}]}]}]}]}]}]}
    item = parse_image_scan(json.dumps(scan), "redis:7-alpine", "3 weeks ago")
    assert item.source == Source.IMAGE and [v.id for v in item.vulns_fixed] == ["CVE-1"]
    assert [v.id for v in item.vulns_remaining] == ["CVE-2"]
    assert upgrade_argv(item, load_settings()) == ["docker", "pull", "redis:7-alpine"]
    assert parse_image_scan("{}", "x:1", "") is None


def test_find_container_files_skips_node_modules(tmp_path):
    (tmp_path / "svc").mkdir()
    (tmp_path / "svc" / "Dockerfile").write_text("FROM node:22\n")
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    (tmp_path / "node_modules" / "x").mkdir(parents=True)
    (tmp_path / "node_modules" / "x" / "Dockerfile").write_text("FROM evil\n")
    names = sorted(p.name for p in find_container_files(tmp_path))
    assert names == ["Dockerfile", "docker-compose.yml"]
