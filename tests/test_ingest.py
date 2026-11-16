"""Generalised source import: finding the pages, then reading them.

The thing being replaced is nine hand-written scripts, one per source, each
hand-rolling CSS selectors for one site's markup. What makes replacing them
possible is that recipe sites publish schema.org JSON-LD because search
engines require it — so the parsing was never the per-site part. The per-site
part was knowing which pages to ask for, and these cover that.

What is asserted here is mostly about restraint: a sitemap that points at
another domain, a document that declares entities, a site that asked crawlers
to stay out. An importer that is careless about any of those is one that
attributes somebody else's recipes to a source, hangs on a malicious file, or
gets the platform blocked.
"""

from __future__ import annotations

import pytest

from recipe_wrangler.ingest import discovery, harvest
from recipe_wrangler.utils.recipe_url import RecipeUrlError

SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://food.test/recipes/soup</loc></url>
  <url><loc>https://food.test/recipes/stew</loc></url>
  <url><loc>https://food.test/about-us</loc></url>
  <url><loc>https://food.test/recipes/soup</loc></url>
</urlset>"""

INDEX = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://food.test/sitemap-recipes.xml</loc></sitemap>
  <sitemap><loc>https://elsewhere.test/sitemap.xml</loc></sitemap>
</sitemapindex>"""

RSS = """<?xml version="1.0"?><rss version="2.0"><channel>
  <item><title>Soup</title><link>https://food.test/recipes/soup</link></item>
  <item><title>Stew</title><link>https://food.test/recipes/stew</link></item>
</channel></rss>"""

ATOM = """<feed xmlns="http://www.w3.org/2005/Atom">
  <entry><link rel="edit" href="/edit/1"/><link rel="alternate" href="/recipes/soup"/></entry>
  <entry><link href="/recipes/stew"/></entry>
</feed>"""


@pytest.fixture
def fetched(monkeypatch):
    """Route every fetch to a dict of canned documents."""
    pages: dict[str, tuple[str, str]] = {}

    def fake(url, *, timeout=10.0, accept=None, content_types=("html",),
             max_bytes=None):
        if url not in pages:
            raise RecipeUrlError(f"404 for {url}")
        return pages[url]

    monkeypatch.setattr(discovery, "fetch_bounded", fake)
    return pages


class TestDiscovery:
    def test_a_sitemap_becomes_a_list_of_pages(self, fetched):
        fetched["https://food.test/sitemap.xml"] = (SITEMAP, "https://food.test/sitemap.xml")
        found = discovery.discover("https://food.test/sitemap.xml")
        assert found.kind == "sitemap"
        assert found.urls == [
            "https://food.test/recipes/soup",
            "https://food.test/recipes/stew",
            "https://food.test/about-us",
        ], "duplicates collapse, order is the document's"

    def test_a_pattern_is_what_turns_a_sitemap_into_a_recipe_import(self, fetched):
        """Most sitemaps list the whole site; the filter is the difference
        between the recipes and every page the site has."""
        fetched["https://food.test/sitemap.xml"] = (SITEMAP, "https://food.test/sitemap.xml")
        found = discovery.discover("https://food.test/sitemap.xml", include=r"/recipes/")
        assert found.urls == ["https://food.test/recipes/soup",
                              "https://food.test/recipes/stew"]
        assert found.considered == 4, "and it reports what it looked at"

    def test_an_index_is_followed_into_its_children(self, fetched):
        fetched["https://food.test/sitemap.xml"] = (INDEX, "https://food.test/sitemap.xml")
        fetched["https://food.test/sitemap-recipes.xml"] = (
            SITEMAP, "https://food.test/sitemap-recipes.xml")
        found = discovery.discover("https://food.test/sitemap.xml", include=r"/recipes/")
        assert found.kind == "sitemap-index"
        assert "https://food.test/recipes/soup" in found.urls
        assert "https://food.test/sitemap-recipes.xml" in found.sources

    def test_a_sitemap_pointing_at_another_domain_is_not_followed(self, fetched):
        """A source vouches for its own pages. Following this would quietly
        attribute a third party's recipes to it."""
        fetched["https://food.test/sitemap.xml"] = (INDEX, "https://food.test/sitemap.xml")
        fetched["https://food.test/sitemap-recipes.xml"] = (
            SITEMAP, "https://food.test/sitemap-recipes.xml")
        fetched["https://elsewhere.test/sitemap.xml"] = (
            SITEMAP, "https://elsewhere.test/sitemap.xml")
        found = discovery.discover("https://food.test/sitemap.xml")
        assert "https://elsewhere.test/sitemap.xml" not in found.sources

    def test_an_unreadable_child_does_not_fail_the_whole_source(self, fetched):
        fetched["https://food.test/sitemap.xml"] = (INDEX, "https://food.test/sitemap.xml")
        # sitemap-recipes.xml is deliberately absent — one part of the site we
        # cannot see is not a failed import.
        found = discovery.discover("https://food.test/sitemap.xml")
        assert found.urls == []
        assert found.kind == "sitemap-index"

    def test_rss_and_atom_both_work(self, fetched):
        fetched["https://food.test/rss"] = (RSS, "https://food.test/rss")
        fetched["https://food.test/atom"] = (ATOM, "https://food.test/atom")
        assert discovery.discover("https://food.test/rss").kind == "rss"
        atom = discovery.discover("https://food.test/atom")
        assert atom.kind == "atom"
        # The alternate link rather than the edit link; relative hrefs
        # resolved; and an entry whose link carries no `rel` still counts,
        # because RFC 4287 says an absent rel must be read as "alternate".
        assert atom.urls == ["https://food.test/recipes/soup",
                             "https://food.test/recipes/stew"]

    def test_a_plain_list_works_and_ignores_comments(self, fetched):
        fetched["https://food.test/urls.txt"] = (
            "# our recipes\nhttps://food.test/recipes/soup\n\n/recipes/stew\n",
            "https://food.test/urls.txt")
        found = discovery.discover("https://food.test/urls.txt")
        assert found.kind == "list"
        assert found.urls == ["https://food.test/recipes/soup",
                              "https://food.test/recipes/stew"]

    def test_a_document_declaring_entities_is_refused(self, fetched):
        """Billion laughs needs only internal entities, which ElementTree
        expands. Refusing the declaration is cheaper than reasoning about it."""
        fetched["https://food.test/evil.xml"] = (
            '<!DOCTYPE lolz [<!ENTITY a "aaaaaaaaaa">]><urlset><url><loc>&a;</loc></url></urlset>',
            "https://food.test/evil.xml")
        with pytest.raises(discovery.DiscoveryError, match="DOCTYPE"):
            discovery.discover("https://food.test/evil.xml")

    def test_something_that_is_not_a_sitemap_says_what_it_expected(self, fetched):
        fetched["https://food.test/x.xml"] = ("<html><body/></html>", "https://food.test/x.xml")
        with pytest.raises(discovery.DiscoveryError, match="urlset"):
            discovery.discover("https://food.test/x.xml")

    def test_the_limit_is_reported_rather_than_silently_applied(self, fetched):
        fetched["https://food.test/sitemap.xml"] = (SITEMAP, "https://food.test/sitemap.xml")
        found = discovery.discover("https://food.test/sitemap.xml", limit=2)
        assert len(found.urls) == 2
        assert found.truncated is True

    def test_urls_from_list_deduplicates_and_keeps_order(self):
        assert discovery.urls_from_list(
            [" b ", "a", "b", "", None]) == ["b", "a"]


RECIPE_HTML = """<html><head><script type="application/ld+json">
{"@context":"https://schema.org","@type":"Recipe","name":"Lentil Soup",
 "recipeIngredient":["200g lentils","1 onion"],
 "recipeInstructions":[{"@type":"HowToStep","text":"Simmer."}],
 "totalTime":"PT30M","recipeYield":"4 servings"}
</script></head><body></body></html>"""


class TestHarvest:
    @pytest.fixture
    def site(self, monkeypatch):
        pages = {}
        robots = {"https://food.test/robots.txt": "User-agent: *\nDisallow: /drafts/\n"}

        def fake(url, *, timeout=10.0, accept=None, content_types=("html",),
                 max_bytes=None):
            if url.endswith("/robots.txt"):
                if url in robots:
                    return robots[url], url
                raise RecipeUrlError("no robots")
            if url not in pages:
                raise RecipeUrlError("Recipe URL did not return HTML")
            return pages[url], url

        monkeypatch.setattr(harvest, "fetch_bounded", fake)
        return pages

    def test_pages_that_parse_become_recipes(self, site):
        site["https://food.test/recipes/soup"] = RECIPE_HTML
        report = harvest.harvest(["https://food.test/recipes/soup"],
                                 delay=0, sleep=lambda _s: None)
        assert report.summary()["imported"] == 1
        assert report.recipes[0]["title"] == "Lentil Soup"
        assert report.recipes[0]["duration"] == 30

    def test_one_bad_page_does_not_end_the_import(self, site):
        """A source of several hundred recipes always contains drafts and
        redirects; the import is the ones that worked plus an honest count."""
        site["https://food.test/recipes/soup"] = RECIPE_HTML
        site["https://food.test/recipes/plain"] = "<html><body>no markup</body></html>"
        report = harvest.harvest([
            "https://food.test/recipes/soup",
            "https://food.test/recipes/plain",
            "https://food.test/recipes/missing",
        ], delay=0, sleep=lambda _s: None)
        summary = report.summary()
        assert summary == {**summary, "attempted": 3, "imported": 1, "failed": 2}
        assert summary["failure_reasons"], "and it says why, grouped"

    def test_robots_is_honoured_because_this_is_crawling(self, site):
        """One person pasting one link is their own visit. Fetching hundreds of
        pages is crawling somebody's site."""
        site["https://food.test/drafts/secret"] = RECIPE_HTML
        report = harvest.harvest(["https://food.test/drafts/secret"],
                                 delay=0, sleep=lambda _s: None)
        assert report.blocked_by_robots == 1
        assert report.recipes == []
        assert "robots.txt" in report.failures[0].reason

    def test_an_unreadable_robots_allows_rather_than_blocks_the_source(self, monkeypatch):
        """Refusing a whole source because a file 404s would be wrong far more
        often than it would be careful."""
        def fake(url, *, timeout=10.0, accept=None, content_types=("html",),
                 max_bytes=None):
            if url.endswith("/robots.txt"):
                raise RecipeUrlError("no robots here")
            return RECIPE_HTML, url
        monkeypatch.setattr(harvest, "fetch_bounded", fake)
        report = harvest.harvest(["https://food.test/recipes/soup"],
                                 delay=0, sleep=lambda _s: None)
        assert report.summary()["imported"] == 1

    def test_requests_are_paced(self, site):
        """A source worth importing is usually a small organisation on modest
        hosting; taking their catalogue at network speed is an outage."""
        site["https://food.test/a"] = RECIPE_HTML
        site["https://food.test/b"] = RECIPE_HTML
        slept = []
        harvest.harvest(["https://food.test/a", "https://food.test/b"],
                        delay=1.5, sleep=slept.append)
        assert slept == [1.5, 1.5]

    def test_the_limit_stops_early(self, site):
        for i in range(5):
            site[f"https://food.test/{i}"] = RECIPE_HTML
        report = harvest.harvest([f"https://food.test/{i}" for i in range(5)],
                                 delay=0, limit=2, sleep=lambda _s: None)
        assert report.summary()["attempted"] == 2

    def test_progress_is_reported_as_it_goes(self, site):
        site["https://food.test/a"] = RECIPE_HTML
        seen = []
        harvest.harvest(["https://food.test/a"], delay=0,
                        on_progress=seen.append, sleep=lambda _s: None)
        assert len(seen) == 1 and seen[0].ok

    def test_an_incomplete_recipe_is_imported_but_counted(self, site):
        """Missing a serving count is not a reason to drop a recipe; it is a
        reason to say so."""
        site["https://food.test/a"] = RECIPE_HTML.replace(
            ',"recipeYield":"4 servings"', "")
        report = harvest.harvest(["https://food.test/a"], delay=0,
                                 sleep=lambda _s: None)
        assert report.summary()["imported"] == 1
        assert report.summary()["incomplete"] == 1


class TestSourceLicensing:
    def test_a_sources_licence_is_recorded_not_guessed(self):
        from recipe_wrangler.catalog import sources

        assert sources.license_for("MyPlate") == "public-domain"
        assert sources.license_for("user") == "user-owned"
        # Undetermined is the honest state for a corpus assembled before
        # anyone asked, and it must not read as permissive.
        assert sources.license_for("HealthyFoods") is None

    def test_the_corpus_can_say_what_it_has_not_established(self):
        from recipe_wrangler.catalog import sources

        undetermined = sources.undetermined_licence_slugs()
        assert "healthyfoods" in undetermined
        assert "myplate" not in undetermined
        assert "recipe1m" not in undetermined, "retired sources are not exposure"


# ------------------------------------------------- discovery → profiling --

RECIPE_PAGE = """<html><head><script type="application/ld+json">
{"@context":"https://schema.org","@type":"Recipe","name":"Lentil Soup",
 "recipeIngredient":["200g lentils","1 onion"],
 "recipeInstructions":[{"@type":"HowToStep","text":"Simmer."}],
 "totalTime":"PT30M","recipeYield":"4"}
</script></head></html>"""

BARE_PAGE = "<html><body>no structured data here</body></html>"


class TestTheImportPipeline:
    """The run that turns a sitemap into profiled recipes.

    `recipe_create` is injected rather than imported: these assert the
    pipeline's decisions — what it refuses, what it counts, what it reports —
    not that the seven-step profiling chain works, which has its own tests and
    needs Neo4j, Postgres and Elasticsearch to say anything.
    """

    @pytest.fixture(autouse=True)
    def no_persistence(self, monkeypatch):
        """The run store needs Postgres; these tests are about the logic."""
        from recipe_wrangler.ingest import pipeline as pipe

        saved = []
        monkeypatch.setattr(pipe.runs, "save", lambda state, **kw: saved.append(
            (state.status, state.imported, state.failed)))
        return saved

    @pytest.fixture
    def site(self, monkeypatch):
        from recipe_wrangler.ingest import pipeline as pipe

        pages = {
            "https://food.test/recipes/soup": RECIPE_PAGE,
            "https://food.test/recipes/stew": RECIPE_PAGE,
            "https://food.test/recipes/draft": BARE_PAGE,
        }

        def fake_discover(location, **kw):
            return discovery.Discovered(
                kind="sitemap", urls=list(pages), considered=len(pages))

        def fake_fetch(url, **kw):
            if url not in pages:
                raise RecipeUrlError("Recipe URL did not return HTML")
            return pages[url], url

        monkeypatch.setattr(pipe.discovery, "discover", fake_discover)
        monkeypatch.setattr(pipe, "fetch_bounded", fake_fetch)
        monkeypatch.setattr(pipe, "_robots", lambda: None)
        return pages

    @pytest.mark.asyncio
    async def test_a_dry_run_reads_without_writing(self, site):
        """The setting to use first: it says how many of a source's pages
        carry usable markup, which is what decides whether it is importable."""
        from recipe_wrangler.ingest import pipeline as pipe

        created = []
        state = await pipe.run_import(
            run_id="r1", location="https://food.test/sitemap.xml",
            dry_run=True, delay=0,
            create_recipe=lambda recipe, region: created.append(recipe),
        )
        assert state["status"] == "succeeded"
        assert state["imported"] == 2
        assert state["failed"] == 1, "the page with no markup"
        assert created == [], "a dry run writes nothing"
        assert state["detail"]["samples"], "and shows what it found"

    @pytest.mark.asyncio
    async def test_a_real_run_profiles_each_recipe(self, site):
        from recipe_wrangler.ingest import pipeline as pipe

        created = []

        async def create(recipe, region):
            created.append((recipe["title"], region))

        state = await pipe.run_import(
            run_id="r2", location="https://food.test/sitemap.xml",
            dry_run=False, delay=0, region="HU", create_recipe=create)
        assert state["imported"] == 2
        assert created == [("Lentil Soup", "HU"), ("Lentil Soup", "HU")]

    @pytest.mark.asyncio
    async def test_a_page_missing_instructions_is_refused_not_profiled(self, monkeypatch):
        """The same bar the single-URL import applies. A recipe with no
        instructions is not one we can profile or show."""
        from recipe_wrangler.ingest import pipeline as pipe

        incomplete = RECIPE_PAGE.replace(
            '"recipeInstructions":[{"@type":"HowToStep","text":"Simmer."}],', "")
        monkeypatch.setattr(pipe.discovery, "discover", lambda location, **kw:
                            discovery.Discovered(kind="sitemap",
                                                 urls=["https://food.test/a"]))
        monkeypatch.setattr(pipe, "fetch_bounded",
                            lambda url, **kw: (incomplete, url))
        monkeypatch.setattr(pipe, "_robots", lambda: None)

        created = []
        state = await pipe.run_import(
            run_id="r3", location="x", dry_run=False, delay=0,
            create_recipe=lambda r, region: created.append(r))
        assert state["imported"] == 0 and state["failed"] == 1
        assert created == []
        assert any("missing" in key for key in state["detail"]["reasons"])

    @pytest.mark.asyncio
    async def test_one_failing_recipe_does_not_end_the_run(self, site):
        from recipe_wrangler.ingest import pipeline as pipe

        calls = {"n": 0}

        async def flaky(recipe, region):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("Neo4j said no")

        state = await pipe.run_import(
            run_id="r4", location="x", dry_run=False, delay=0,
            create_recipe=flaky)
        assert state["status"] == "succeeded"
        assert state["imported"] == 1 and state["failed"] == 2

    @pytest.mark.asyncio
    async def test_discovering_nothing_fails_with_advice(self, monkeypatch):
        from recipe_wrangler.ingest import pipeline as pipe

        monkeypatch.setattr(pipe.discovery, "discover", lambda location, **kw:
                            discovery.Discovered(kind="sitemap", urls=[],
                                                 considered=4000))
        state = await pipe.run_import(run_id="r5", location="x", delay=0)
        assert state["status"] == "failed"
        assert "include pattern" in state["error"]
        assert "4000" in state["error"], "and says what it did see"

    @pytest.mark.asyncio
    async def test_a_source_that_cannot_be_reached_fails_the_run_not_the_request(
            self, monkeypatch):
        from recipe_wrangler.ingest import pipeline as pipe

        def boom(location, **kw):
            raise RecipeUrlError("Recipe host could not be resolved")
        monkeypatch.setattr(pipe.discovery, "discover", boom)
        state = await pipe.run_import(run_id="r6", location="x", delay=0)
        assert state["status"] == "failed"
        assert "resolved" in state["error"]

    @pytest.mark.asyncio
    async def test_robots_is_counted_as_skipped_not_failed(self, site, monkeypatch):
        """A page we were asked not to fetch is not a broken page."""
        from recipe_wrangler.ingest import pipeline as pipe

        class DenyAll:
            def allows(self, url):
                return False
        monkeypatch.setattr(pipe, "_robots", lambda: DenyAll())

        state = await pipe.run_import(
            run_id="r7", location="x", dry_run=True, delay=0)
        assert state["skipped"] == 3
        assert state["failed"] == 0 and state["imported"] == 0

    def test_an_unknown_source_slug_resolves_to_nothing(self):
        from recipe_wrangler.ingest import pipeline as pipe

        assert pipe.resolve_source("not-a-source") is None
        assert pipe.resolve_source("myplate").slug == "myplate"
