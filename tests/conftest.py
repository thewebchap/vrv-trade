import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tradepilot.local_model import ModelResponse  # noqa: E402
from tradepilot.models import Document, Page  # noqa: E402


class FakeModel:
    """Stands in for the local model. `answers` is a list of dicts (or None
    to simulate failure) returned in order; `error` is used for failures."""

    name = "fake-model"

    def __init__(self, answers=None, error="model unavailable"):
        self.answers = list(answers or [])
        self.error = error
        self.prompts = []

    def ask_json(self, prompt, images=None, max_tokens=0):
        self.prompts.append(prompt)
        data = self.answers.pop(0) if self.answers else None
        return ModelResponse(data, None if data is not None else self.error, self.name)


@pytest.fixture
def fake_model():
    return FakeModel


def make_doc(text: str, filename: str = "x.pdf", doc_type: str = "UNKNOWN", method: str = "pdf_text") -> Document:
    pages = [Page(i + 1, t, method) for i, t in enumerate(text.split("\f"))]
    return Document(filename, filename, pages, doc_type=doc_type)
