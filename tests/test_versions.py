from bosun.versions import Version, is_newer, jump


def test_semver_ordering_with_prerelease():
    assert Version("8.0.0-rc.6") < Version("8.0.0")
    assert Version("1.13.5") < Version("1.13.6") < Version("1.14.4")
    assert Version("v0.70.0") < Version("v0.81.0")


def test_padding_and_windows_versions():
    assert Version("1.2") == Version("1.2.0")
    assert Version("143.2.7499.37654") < Version("155.0.8059.150")


def test_debian_revision_is_not_prerelease():
    assert Version("2.80.0-6ubuntu3.9") < Version("2.80.0-6ubuntu3.10")


def test_build_number_in_parens():
    assert Version("7.1.9 (48550)") < Version("7.2.1 (48556)")


def test_is_newer_handles_scheme_changes_and_unknowns():
    # npm reported latest 1.0.0 for a package at 2025.7.1 (calver -> semver reset)
    assert not is_newer("1.0.0", "2025.7.1")
    assert is_newer("590.0.0", "Unknown")
    assert is_newer("3.14.7", "< 3.14.7")


def test_jump():
    assert jump("2.50.0", "2.55.0.5") == "minor"
    assert jump("4.55.0", "5.0.1") == "major"
    assert jump("22.23.2", "22.23.4") == "patch"
    assert jump("Unknown", "1.0") == "unknown"
