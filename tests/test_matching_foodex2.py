import zipfile

from recipe_wrangler.matching.foodex2 import FoodEx2Catalogue


def catalogue(tmp_path):
    path = tmp_path / "MTX.ecf"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("MTX.xml", '''<message><catalogue>
          <catalogueVersion><version>test</version></catalogueVersion><terms>
          <term><termDesc><termCode>A000A</termCode><termExtendedName>Food</termExtendedName></termDesc>
          <termVersion><status>APPROVED</status></termVersion>
          <hierarchyAssignments><hierarchyAssignment><hierarchyCode>report</hierarchyCode><parentCode>root</parentCode></hierarchyAssignment></hierarchyAssignments>
          </term><term><termDesc><termCode>A077L</termCode><termExtendedName>Sugar free</termExtendedName></termDesc>
          <termVersion><status>DEPRECATED</status></termVersion></term>
          </terms></catalogue></message>''')
    return FoodEx2Catalogue(path)


def test_resolves_explicit_codes_with_version_and_status(tmp_path):
    result = catalogue(tmp_path).resolve("A000A#F04.A077L")
    assert [term.label for term in result.terms] == ["Food", "Sugar free"]
    assert result.catalogue_version == "test"
    assert len(result.catalogue_sha256) == 64
    assert result.terms[0].parents == (("report", "root"),)
    assert result.issues == ("foodex2:term_status:A077L:DEPRECATED",)


def test_missing_unknown_and_malformed_annotations_remain_explicit(tmp_path):
    source = catalogue(tmp_path)
    assert source.resolve("").issues == ("foodex2:missing_annotation",)
    assert source.resolve("ZZZZZ").issues == ("foodex2:unknown_code:ZZZZZ",)
    assert source.resolve("A000A some text").issues == ("foodex2:unsupported_expression",)
    assert source.resolve("A000A some text").terms == ()
