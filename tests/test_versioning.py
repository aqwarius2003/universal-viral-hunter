from src.version import get_version, get_git_commit, get_version_info


def test_version_retrieval():
    ver = get_version()
    assert ver.count(".") >= 1
    info = get_version_info()
    assert "version" in info
    assert info["version"].startswith("v")
    assert "commit" in info
    assert "full_string" in info
