from bosun.enrich.kev import apply_kev
from bosun.enrich.summarize import heuristic_summary
from bosun.models import Item, Source, Summary, Tier, Vuln
from bosun.scoring import score


def item(**kw) -> Item:
    base = dict(source=Source.WINGET, package_id="Foo.Bar", name="Foo", installed="1.0.0", available="1.0.1")
    return Item(**{**base, **kw})


def test_kev_marks_by_alias_and_forces_critical():
    it = item(vulns_fixed=(Vuln("GHSA-x", aliases=("CVE-2026-109618",), cvss=6.0),))
    (marked,) = apply_kev([it], {"CVE-2026-109618"})
    scored = score(marked)
    assert marked.vulns_fixed[0].kev
    assert scored.tier == Tier.CRITICAL
    assert any("actively exploited" in r for r in scored.reasons)


def test_cvss_bands():
    assert score(item(vulns_fixed=(Vuln("CVE-1", cvss=9.8),))).tier == Tier.CRITICAL
    assert score(item(vulns_fixed=(Vuln("CVE-1", cvss=7.5),))).tier == Tier.HIGH
    assert score(item()).tier == Tier.LOW


def test_security_pocket_is_high():
    assert score(item(source=Source.WSL_APT, security_pocket=True)).tier == Tier.HIGH


def test_summary_value_and_breaking_reason():
    s = Summary(headline="x", upgrade_value="high", breaking_changes=("drops Node 18",))
    out = score(item(summary=s, installed="1.0", available="2.0"))
    assert out.tier == Tier.MEDIUM
    assert "has breaking changes - read before upgrading" in out.reasons
    assert "major version behind" in out.reasons


def test_heuristic_summary_detects_security_and_breaking():
    s = heuristic_summary(item(), "Fixed CVE-2026-1234.\n\nBREAKING CHANGE: config moved")
    assert s.upgrade_value == "medium" and s.security_fixes and s.breaking_changes
    assert heuristic_summary(item(), "").upgrade_value == "low"


def test_heuristic_value_follows_cvss_and_low_cvss_is_not_high():
    assert heuristic_summary(item(vulns_fixed=(Vuln("CVE-1", cvss=9.1),)), "").upgrade_value == "critical"
    assert heuristic_summary(item(vulns_fixed=(Vuln("CVE-1", cvss=7.5),)), "").upgrade_value == "high"
    low = item(vulns_fixed=(Vuln("CVE-1", cvss=3.3), Vuln("CVE-2", cvss=3.3)))
    low = low.with_(summary=heuristic_summary(low, ""))
    assert score(low).tier == Tier.MEDIUM
