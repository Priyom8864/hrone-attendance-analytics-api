"""Allow pure helper tests to run without PyMongo installed.

When PyMongo is installed, the real package is used. When it is absent, this
file provides import-only stubs; they do not implement database operations and
must never be used to claim integration coverage.
"""
import importlib.util
import sys
import types


if importlib.util.find_spec("pymongo") is None:
    pymongo = types.ModuleType("pymongo")
    pymongo.ASCENDING = 1
    pymongo.DESCENDING = -1

    class _ReturnDocument:
        AFTER = True
        BEFORE = False

    pymongo.ReturnDocument = _ReturnDocument

    class _UnavailableCollection:
        def __getattr__(self, name):
            raise RuntimeError(
                "PyMongo is unavailable; database operations cannot run in helper tests"
            )

    class _UnavailableDatabase:
        def __getattr__(self, name):
            return _UnavailableCollection()

    class _UnavailableClient:
        def __init__(self, *args, **kwargs):
            pass

        def __getitem__(self, name):
            return _UnavailableDatabase()

    pymongo.MongoClient = _UnavailableClient

    errors = types.ModuleType("pymongo.errors")

    class PyMongoError(Exception):
        pass

    class DuplicateKeyError(PyMongoError):
        pass

    errors.PyMongoError = PyMongoError
    errors.DuplicateKeyError = DuplicateKeyError
    pymongo.errors = errors
    sys.modules["pymongo"] = pymongo
    sys.modules["pymongo.errors"] = errors
