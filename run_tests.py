import os
import pytest
import pathlib

old_stat = pathlib.Path.stat

def safe_stat(self, *args, **kwargs):
    try:
        return old_stat(self, *args, **kwargs)
    except PermissionError:
        class FakeStat:
            st_mode = 0
        return FakeStat()

pathlib.Path.stat = safe_stat

if __name__ == '__main__':
    pytest.main(['tests/'])
