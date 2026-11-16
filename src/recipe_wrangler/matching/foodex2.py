"""Read-only FoodEx2 catalogue resolution; classifications are supporting evidence."""

from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class FoodEx2Term:
    code: str
    label: str
    status: str
    scope_note: str
    parents: tuple[tuple[str, str], ...]
    implicit_facets: tuple[str, ...]


@dataclass(frozen=True)
class FoodEx2Evidence:
    expression: str
    catalogue_version: str
    catalogue_sha256: str
    terms: tuple[FoodEx2Term, ...]
    issues: tuple[str, ...]


class FoodEx2Catalogue:
    """Resolve actual codes without predicting links or inferring absent facets."""

    def __init__(self, path: Path) -> None:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        self.sha256 = digest.hexdigest()
        self.version = ""
        self.terms: dict[str, FoodEx2Term] = {}
        with zipfile.ZipFile(path) as archive, archive.open("MTX.xml") as stream:
            for _, element in ET.iterparse(stream, events=("end",)):
                if element.tag == "catalogueVersion":
                    self.version = element.findtext("version") or ""
                elif element.tag == "term":
                    code = element.findtext("termDesc/termCode") or ""
                    if not code or code in self.terms:
                        raise ValueError(f"Missing or duplicate FoodEx2 term: {code!r}")
                    implicit = tuple(
                        value.text or ""
                        for attribute in element.findall("implicitAttributes/implicitAttribute")
                        if attribute.findtext("attributeCode") in {"implicitFacets", "allFacets"}
                        for value in attribute.findall("attributeValues/attributeValue")
                        if value.text
                    )
                    self.terms[code] = FoodEx2Term(
                        code=code,
                        label=element.findtext("termDesc/termExtendedName") or "",
                        status=element.findtext("termVersion/status") or "",
                        scope_note=element.findtext("termDesc/termScopeNote") or "",
                        parents=tuple(
                            (assignment.findtext("hierarchyCode") or "", assignment.findtext("parentCode") or "")
                            for assignment in element.findall("hierarchyAssignments/hierarchyAssignment")
                        ),
                        implicit_facets=implicit,
                    )
                    element.clear()
        if not self.version or not self.terms:
            raise ValueError("FoodEx2 catalogue must contain a version and terms")

    def resolve(self, expression: str) -> FoodEx2Evidence:
        """Resolve a base code and explicit descriptors, retaining issues for review.

        Implicit catalogue expressions are exposed unchanged; their release
        compatibility and interpretation must be validated before use as gates.
        """
        expression = expression.strip()
        terms: list[FoodEx2Term] = []
        issues: list[str] = []
        if not expression:
            issues.append("foodex2:missing_annotation")
        elif not re.fullmatch(r"[A-Z0-9]{5}(?:#F\d{2}\.[A-Z0-9]{5}(?:\$F\d{2}\.[A-Z0-9]{5})*)?", expression):
            issues.append("foodex2:unsupported_expression")
        else:
            codes = [expression[:5], *re.findall(r"F\d{2}\.([A-Z0-9]{5})", expression)]
            for code in dict.fromkeys(codes):
                term = self.terms.get(code)
                if term is None:
                    issues.append(f"foodex2:unknown_code:{code}")
                else:
                    terms.append(term)
                    if term.status != "APPROVED":
                        issues.append(f"foodex2:term_status:{code}:{term.status}")
        return FoodEx2Evidence(expression, self.version, self.sha256, tuple(terms), tuple(issues))
