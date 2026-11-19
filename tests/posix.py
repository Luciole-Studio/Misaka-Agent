"""Marks for tests of behaviour only POSIX has."""
import os

import pytest

modes = pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits; Windows keeps a profile private by ACL")
