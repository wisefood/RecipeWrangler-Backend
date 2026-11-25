import builtins
from pathlib import Path
import runpy
import sys
from types import ModuleType

PATH = Path(__file__).resolve().parents[1] / 'src/recipe_wrangler/utils/get_embeddings.py'


def test_cpu_only_import_does_not_load_embedding_backend(monkeypatch):
    monkeypatch.setenv('EMBED_WARM_ON_IMPORT', 'false')
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == 'langchain_huggingface':
            raise AssertionError('Embedding backend should load only on use')
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', guarded_import)
    module = runpy.run_path(str(PATH))
    assert callable(module['get_embeddings'])


def test_first_embedding_call_loads_and_reuses_backend(monkeypatch):
    monkeypatch.setenv('EMBED_WARM_ON_IMPORT', 'false')
    constructions = []

    class FakeEmbedder:
        def __init__(self, **kwargs):
            constructions.append(kwargs)

        def embed_query(self, text):
            return [len(text)]

    backend = ModuleType('langchain_huggingface')
    backend.HuggingFaceEmbeddings = FakeEmbedder
    monkeypatch.setitem(sys.modules, 'langchain_huggingface', backend)
    module = runpy.run_path(str(PATH))
    assert constructions == []
    assert module['get_embeddings']('abc', 'test-model') == [3]
    assert module['get_embeddings']('abcd', 'test-model') == [4]
    assert len(constructions) == 1
