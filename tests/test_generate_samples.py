import pdfplumber
import pikepdf
import pytest

from samples.generate import generate_all
from samples.sample_data import ALL_SAMPLES, PASSWORDS


@pytest.fixture(scope="module")
def out(tmp_path_factory):
    path = tmp_path_factory.mktemp("samples")
    generate_all(path)
    return path


def test_every_sample_has_pdf_and_expected_json(out):
    for name in ALL_SAMPLES:
        assert (out / "pdfs" / f"{name}.pdf").exists()
        assert (out / "expected" / f"{name}.json").exists()


def test_password_protected_pdf_is_really_locked(out):
    path = out / "pdfs" / "xyz_wealth.pdf"
    with pytest.raises(pikepdf.PasswordError):
        pikepdf.open(path)
    with pikepdf.open(path, password=PASSWORDS["xyz_wealth"]):
        pass


def test_pdf_text_contains_provider_and_client_id(out):
    for name, build in ALL_SAMPLES.items():
        s = build()
        with pdfplumber.open(out / "pdfs" / f"{name}.pdf", password=PASSWORDS.get(name)) as pdf:
            text = "\n".join(p.extract_text() or "" for p in pdf.pages)
        assert s.provider in text, name
        assert s.client_id in text, name