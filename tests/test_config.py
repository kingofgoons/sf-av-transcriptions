"""Tier 0 tests for the deployment config template-and-copy mechanism.

ZERO COST, FULLY OFFLINE. No Snowflake connection, no credits, no human. Run these
before anything reaches the account:

    pytest tests/test_config.py -v

WHAT THIS PROTECTS

The config mechanism decides which objects every script in the project touches,
including `999_teardown.sql`, which issues `DROP DATABASE`. A silent config error is
therefore not a cosmetic bug. Two failure modes are specifically covered:

  1. The extraction of 00_config.sql.template silently changed a value.
     -> test_template_matches_preextract_fixture

  2. A stale staged config deploys the PREVIOUS values while the operator believes
     their edits are live. This is the same class of failure as the 2026-08-18
     V1/V2 grant drift; centralising the values fixed the copies but moved the
     drift to "local versus staged".
     -> the revision-hash tests, which make staleness mechanically detectable

`snow` is never invoked. Tests that exercise publish_config.sh put a fake `snow` on
PATH that captures what WOULD be staged, so the full stamping path is verified
without touching Snowflake.
"""

import os
import shutil
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
TEMPLATE = SCRIPTS / "00_config.sql.template"
FIXTURE = REPO / "tests" / "fixtures" / "00_config.sql.preextract"

INIT = SCRIPTS / "init_config.sh"
REVISION = SCRIPTS / "config_revision.sh"
FRESH = SCRIPTS / "check_config_fresh.sh"
PUBLISH = SCRIPTS / "publish_config.sh"

SENTINEL = "INJECTED_AT_PUBLISH"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def run(cmd, **kw):
    """Run a command, capturing output. Never raises on non-zero."""
    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    return subprocess.run([str(c) for c in cmd], **kw)


def set_lines(path):
    """Executable SET statements only, which is what actually configures anything."""
    return [
        ln for ln in Path(path).read_text().splitlines()
        if ln.startswith("SET ")
    ]


def set_names(path):
    return sorted(
        ln.split()[1] for ln in set_lines(path)
    )


@pytest.fixture
def sandbox(tmp_path):
    """An isolated copy of scripts/ so tests never touch the real working config.

    The real scripts/00_config.sql is an operator's live deployment config. Tests
    that mutate a config must do so on a copy.
    """
    dst = tmp_path / "scripts"
    dst.mkdir()
    for name in ("00_config.sql.template", "init_config.sh", "config_revision.sh",
                 "check_config_fresh.sh", "publish_config.sh"):
        shutil.copy2(SCRIPTS / name, dst / name)
        os.chmod(dst / name, os.stat(dst / name).st_mode | stat.S_IEXEC)
    return dst


@pytest.fixture
def fake_snow(tmp_path):
    """A stand-in for `snow` that records the file a PUT would have uploaded.

    Lets the stamping path be tested end to end with no Snowflake involvement.
    """
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    captured = tmp_path / "staged_capture.sql"
    (bindir / "snow").write_text(textwrap.dedent(f"""\
        #!/usr/bin/env bash
        for a in "$@"; do
          if [[ "$a" == PUT* ]]; then
            src=$(echo "$a" | sed "s|.*file://\\([^']*\\)'.*|\\1|")
            cp "$src" "{captured}"
          fi
        done
        exit 0
        """))
    os.chmod(bindir / "snow", 0o755)
    env = dict(os.environ, PATH=f"{bindir}{os.pathsep}{os.environ['PATH']}")
    return env, captured


# ---------------------------------------------------------------------------
# T0.1  the extraction changed no values
# ---------------------------------------------------------------------------

# SET names added to the template SINCE the extraction, deliberately, each with the
# commit that added it. Additions must be listed here explicitly: that requirement is
# the point, not overhead. It forces a new config value to be a reviewed decision rather
# than something that appears in a diff nobody reads.
#
# Anything NOT on this list that differs from the pre-extraction fixture is still a hard
# failure, so the original guarantee - no silent retargeting of the project - is intact.
ADDED_SINCE_EXTRACTION = {
    # 2026-09-24: compute sizing moved out of 02_setup.sql into config so that a
    # reconciling ALTER can enforce it. The pool had drifted to the 3600s platform
    # default because AUTO_SUSPEND_SECS was never in the DDL at all.
    'PROJECT_POOL_INSTANCE_FAMILY',
    'PROJECT_POOL_MIN_NODES',
    'PROJECT_POOL_MAX_NODES',
    'PROJECT_POOL_AUTO_SUSPEND_SECS',
    'PROJECT_WH_SIZE',
    'PROJECT_WH_AUTO_SUSPEND',

    # 2026-09-25: job-service launch path. The gate procedure can run either the GPU
    # notebook or the headless payload; PROJECT_LAUNCH_MODE selects which, and is also
    # the rollback switch. PAYLOAD_STAGE existed on the account for a day before it
    # existed in code - it was created ad hoc during the port spike - which is exactly
    # the drift the ENFORCED sync rule in agents.md exists to catch.
    'PROJECT_LAUNCH_MODE',
    'PROJECT_STAGE_PAYLOAD',
    'PROJECT_JOB_NAME',
    'PROJECT_JOB_SPEC_FILE',
    'PROJECT_JOB_IMAGE',
    'FQ_STAGE_PAYLOAD',
    'FQ_JOB',
}


def _set_map(path):
    """Map SET name -> full line, so additions and edits can be told apart."""
    out = {}
    for line in set_lines(path):
        name = line.split('=', 1)[0].replace('SET', '', 1).strip()
        out[name] = line
    return out


def test_template_matches_preextract_fixture():
    """Every SET that existed before the extraction must still exist, unchanged.

    This is the regression guard for the whole refactor: a value altered while moving
    00_config.sql to a template would silently retarget every script in the project.

    It permits ADDITIONS listed in ADDED_SINCE_EXTRACTION, because the template is a
    living file and new config values are legitimate. It does NOT permit a pre-existing
    value to change, or any SET to disappear, or an unlisted name to appear.
    """
    assert FIXTURE.exists(), "pre-extraction fixture is missing; regression is unprovable"
    before, after = _set_map(FIXTURE), _set_map(TEMPLATE)

    removed = set(before) - set(after)
    assert not removed, f"SET statements disappeared from the template: {sorted(removed)}"

    added = set(after) - set(before)
    unexpected = added - ADDED_SINCE_EXTRACTION
    assert not unexpected, (
        f"new SET names not declared in ADDED_SINCE_EXTRACTION: {sorted(unexpected)}. "
        "If the addition is intended, add it there with a dated reason.")

    # Every pre-existing name must be byte-identical, except CONFIG_REVISION which the
    # template deliberately carries as the sentinel for publish_config.sh to replace.
    changed = [(n, before[n], after[n]) for n in before
               if before[n] != after[n] and n != 'CONFIG_REVISION']
    assert not changed, f"pre-existing SET values were modified: {changed}"

    assert SENTINEL in after['CONFIG_REVISION'], (
        "the template's CONFIG_REVISION must be the publish sentinel")


def test_declared_additions_are_actually_present():
    """Keeps ADDED_SINCE_EXTRACTION honest.

    Without this, a name could be removed from the template while its entry lingered
    here, and the test above would still pass because it only checks for unexpected
    additions - not for stale declarations.
    """
    after = _set_map(TEMPLATE)
    missing = ADDED_SINCE_EXTRACTION - set(after)
    assert not missing, (
        f"ADDED_SINCE_EXTRACTION lists names absent from the template: {sorted(missing)}")


def test_new_config_values_reach_the_project_config_view():
    """A config value the emitter does not project is invisible to the dashboard and to
    scripts/09_drift_check.sql, which is how the pool's auto-suspend went unchecked.

    The emitter builds V_PROJECT_CONFIG at the bottom of the template. Every compute
    sizing value must appear there, or the drift check silently cannot see it.
    """
    text = TEMPLATE.read_text()
    emitter = text[text.find('CREATE OR REPLACE VIEW'):]
    assert emitter, "could not locate the V_PROJECT_CONFIG emitter in the template"
    for name in sorted(ADDED_SINCE_EXTRACTION):
        assert name in emitter, (
            f"{name} is SET but never projected into V_PROJECT_CONFIG; "
            "the drift check cannot verify it")


def test_template_is_the_only_tracked_config():
    """The working copy must not be version controlled; the template must be."""
    tracked = run(["git", "-C", REPO, "ls-files",
                   "scripts/00_config.sql", "scripts/00_config.sql.template"]).stdout.split()
    assert "scripts/00_config.sql.template" in tracked, "template is not tracked"
    assert "scripts/00_config.sql" not in tracked, (
        "the working config is tracked; local values would be committed"
    )


def test_working_copy_is_gitignored():
    """Including parallel-deployment copies, while NOT ignoring the template."""
    assert run(["git", "-C", REPO, "check-ignore", "scripts/00_config.sql"]).returncode == 0
    assert run(["git", "-C", REPO, "check-ignore", "scripts/00_config_dev.sql"]).returncode == 0
    assert run(["git", "-C", REPO, "check-ignore",
                "scripts/00_config.sql.template"]).returncode != 0, (
        "the template is ignored, so a fresh clone would have no config at all"
    )


# ---------------------------------------------------------------------------
# T0.2 / T0.3  init_config.sh
# ---------------------------------------------------------------------------

def test_init_creates_faithful_copy(sandbox):
    r = run([sandbox / "init_config.sh"], cwd=sandbox)
    assert r.returncode == 0, r.stderr
    assert (sandbox / "00_config.sql").read_text() == (sandbox / "00_config.sql.template").read_text()


def test_init_refuses_to_clobber(sandbox):
    """An edited config is an operator's real deployment. Overwriting it silently
    would point every later script at the wrong objects."""
    run([sandbox / "init_config.sh"], cwd=sandbox)
    target = sandbox / "00_config.sql"
    target.write_text(target.read_text().replace("TRANSCRIPTION_DB_V2", "MY_EDITED_DB"))
    before = target.read_text()

    r = run([sandbox / "init_config.sh"], cwd=sandbox)
    assert r.returncode != 0, "clobbered an existing config"
    assert target.read_text() == before, "existing config was modified"
    assert "MY_EDITED_DB" in target.read_text()


def test_init_force_overwrites(sandbox):
    run([sandbox / "init_config.sh"], cwd=sandbox)
    target = sandbox / "00_config.sql"
    target.write_text("SET PROJECT_DB = 'GONE';\n")
    r = run([sandbox / "init_config.sh", "--force"], cwd=sandbox)
    assert r.returncode == 0, r.stderr
    assert target.read_text() == (sandbox / "00_config.sql.template").read_text()


# ---------------------------------------------------------------------------
# T0.6 / T0.7  revision hash properties
# ---------------------------------------------------------------------------

def revision_of(sandbox, path):
    r = run([sandbox / "config_revision.sh", path], cwd=sandbox)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def test_revision_is_deterministic(sandbox):
    run([sandbox / "init_config.sh"], cwd=sandbox)
    cfg = sandbox / "00_config.sql"
    assert revision_of(sandbox, cfg) == revision_of(sandbox, cfg)


def test_revision_is_12_hex_chars(sandbox):
    run([sandbox / "init_config.sh"], cwd=sandbox)
    rev = revision_of(sandbox, sandbox / "00_config.sql")
    assert len(rev) == 12, rev
    assert all(c in "0123456789abcdef" for c in rev), rev


def test_revision_ignores_comment_edits(sandbox):
    """Critical for usability, not just tidiness.

    The deploy scripts REFUSE to deploy on a revision mismatch. If documenting your
    own config changed the hash, that would look identical to a stale staged copy,
    and operators would learn to ignore the warning.
    """
    run([sandbox / "init_config.sh"], cwd=sandbox)
    cfg = sandbox / "00_config.sql"
    base = revision_of(sandbox, cfg)
    cfg.write_text(cfg.read_text() + "\n-- a newly added explanatory comment\n")
    assert revision_of(sandbox, cfg) == base


def test_revision_ignores_whitespace_realignment(sandbox):
    run([sandbox / "init_config.sh"], cwd=sandbox)
    cfg = sandbox / "00_config.sql"
    base = revision_of(sandbox, cfg)
    cfg.write_text(cfg.read_text().replace(
        "SET PROJECT_DB = ", "SET PROJECT_DB    =    "))
    assert revision_of(sandbox, cfg) == base


def test_revision_ignores_its_own_value(sandbox):
    """CONFIG_REVISION must be excluded from its own input, or it is self-referential."""
    run([sandbox / "init_config.sh"], cwd=sandbox)
    cfg = sandbox / "00_config.sql"
    base = revision_of(sandbox, cfg)
    cfg.write_text(cfg.read_text().replace(
        f"SET CONFIG_REVISION = '{SENTINEL}';", "SET CONFIG_REVISION = 'whatever';"))
    assert revision_of(sandbox, cfg) == base


def test_revision_changes_when_a_value_changes(sandbox):
    run([sandbox / "init_config.sh"], cwd=sandbox)
    cfg = sandbox / "00_config.sql"
    base = revision_of(sandbox, cfg)
    cfg.write_text(cfg.read_text().replace("TRANSCRIPTION_DB_V2", "TRANSCRIPTION_DB_V9"))
    assert revision_of(sandbox, cfg) != base


@pytest.mark.parametrize("args,expected_rc", [
    ([], 2),                       # no argument
    (["/tmp/definitely_missing.sql"], 1),
])
def test_revision_failure_modes(sandbox, args, expected_rc):
    r = run([sandbox / "config_revision.sh", *args], cwd=sandbox)
    assert r.returncode == expected_rc, r.stdout + r.stderr


def test_revision_rejects_a_file_with_no_set_statements(sandbox):
    """Must fail loudly, not emit a hash of nothing. A silent empty revision would
    make the staleness comparison meaningless."""
    junk = sandbox / "junk.sql"
    junk.write_text("-- just a comment, no SET statements\n")
    r = run([sandbox / "config_revision.sh", junk], cwd=sandbox)
    assert r.returncode != 0
    assert "no SET statements" in (r.stdout + r.stderr)


# ---------------------------------------------------------------------------
# T0.4 / T0.5  drift detection
# ---------------------------------------------------------------------------

def test_publish_blocks_when_copy_is_missing_a_template_variable(sandbox, fake_snow):
    """The one real weakness of template-and-copy: a template that gains a variable
    later leaves existing copies incomplete, and scripts then reference an unset
    session variable."""
    env, captured = fake_snow
    run([sandbox / "init_config.sh"], cwd=sandbox)
    cfg = sandbox / "00_config.sql"
    cfg.write_text("\n".join(
        ln for ln in cfg.read_text().splitlines()
        if not ln.startswith("SET PROJECT_APP_ROLE")) + "\n")

    r = run([sandbox / "publish_config.sh"], cwd=sandbox, env=env)
    assert r.returncode != 0, "published despite drift"
    assert "PROJECT_APP_ROLE" in r.stdout + r.stderr
    assert not captured.exists(), "uploaded before failing the drift check"


def test_publish_tolerates_a_local_extra_variable(sandbox, fake_snow):
    """Warn, do not fail: an extra may be deliberate, and refusing would be hostile."""
    env, captured = fake_snow
    run([sandbox / "init_config.sh"], cwd=sandbox)
    cfg = sandbox / "00_config.sql"
    cfg.write_text(cfg.read_text() + "\nSET PROJECT_LOCAL_EXTRA = 'x';\n")

    r = run([sandbox / "publish_config.sh"], cwd=sandbox, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "PROJECT_LOCAL_EXTRA" in r.stdout
    assert captured.exists(), "did not publish"


# ---------------------------------------------------------------------------
# T0.8  stamping
# ---------------------------------------------------------------------------

def test_publish_stamps_the_revision_and_leaves_local_untouched(sandbox, fake_snow):
    env, captured = fake_snow
    run([sandbox / "init_config.sh"], cwd=sandbox)
    cfg = sandbox / "00_config.sql"
    expected = revision_of(sandbox, cfg)

    r = run([sandbox / "publish_config.sh"], cwd=sandbox, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert captured.exists(), "nothing was staged"

    staged = captured.read_text()
    assert f"SET CONFIG_REVISION = '{expected}';" in staged
    assert f"SET CONFIG_REVISION = '{SENTINEL}';" not in staged, "sentinel not substituted"

    # The local file must keep the sentinel: it stays a clean source, and the
    # sentinel remains the tell-tale for a file staged outside publish_config.sh.
    assert f"SET CONFIG_REVISION = '{SENTINEL}';" in cfg.read_text()


def test_staged_and_local_differ_only_by_the_revision(sandbox, fake_snow):
    env, captured = fake_snow
    run([sandbox / "init_config.sh"], cwd=sandbox)
    cfg = sandbox / "00_config.sql"
    run([sandbox / "publish_config.sh"], cwd=sandbox, env=env)

    local = [ln for ln in cfg.read_text().splitlines() if not ln.startswith("SET CONFIG_REVISION")]
    staged = [ln for ln in captured.read_text().splitlines() if not ln.startswith("SET CONFIG_REVISION")]
    assert local == staged


def test_publish_fails_without_a_local_config(sandbox, fake_snow):
    """A fresh clone has no working copy; the error must name init_config.sh."""
    env, _ = fake_snow
    r = run([sandbox / "publish_config.sh"], cwd=sandbox, env=env)
    assert r.returncode != 0
    assert "init_config.sh" in r.stdout + r.stderr


# ---------------------------------------------------------------------------
# T2.2 offline half  staleness detection
# ---------------------------------------------------------------------------

def test_fresh_check_passes_on_matching_revision(sandbox):
    run([sandbox / "init_config.sh"], cwd=sandbox)
    rev = revision_of(sandbox, sandbox / "00_config.sql")
    r = run([sandbox / "check_config_fresh.sh", rev, "@X.Y.Z/00_config.sql"], cwd=sandbox)
    assert r.returncode == 0, r.stdout + r.stderr


def test_fresh_check_fails_on_stale_staged_config(sandbox):
    run([sandbox / "init_config.sh"], cwd=sandbox)
    r = run([sandbox / "check_config_fresh.sh", "deadbeef1234", "@X.Y.Z/00_config.sql"],
            cwd=sandbox)
    assert r.returncode != 0, "a stale staged config was accepted"
    assert "STALE" in r.stdout + r.stderr
    assert "publish_config.sh" in r.stdout + r.stderr, "error does not say how to fix it"


def test_fresh_check_rejects_an_unstamped_staged_config(sandbox):
    run([sandbox / "init_config.sh"], cwd=sandbox)
    r = run([sandbox / "check_config_fresh.sh", SENTINEL, "@X.Y.Z/00_config.sql"], cwd=sandbox)
    assert r.returncode != 0
    assert "sentinel" in (r.stdout + r.stderr).lower()


def test_fresh_check_skips_when_there_is_no_local_config(sandbox):
    """Deploying from a fresh clone against someone else's published config is
    legitimate. Only a local file that DISAGREES is an error."""
    r = run([sandbox / "check_config_fresh.sh", "abc123abc123",
             "@X.Y.Z/00_config_absent.sql"], cwd=sandbox)
    assert r.returncode == 0, r.stdout + r.stderr


def test_fresh_check_compares_against_the_right_parallel_copy(sandbox):
    """With CONFIG_STAGE_PATH pointing at a dev config, the check must compare
    against 00_config_dev.sql, not always 00_config.sql."""
    run([sandbox / "init_config.sh"], cwd=sandbox)
    dev = sandbox / "00_config_dev.sql"
    dev.write_text((sandbox / "00_config.sql").read_text()
                   .replace("TRANSCRIPTION_DB_V2", "TRANSCRIPTION_DB_DEV"))
    dev_rev = revision_of(sandbox, dev)
    main_rev = revision_of(sandbox, sandbox / "00_config.sql")
    assert dev_rev != main_rev

    ok = run([sandbox / "check_config_fresh.sh", dev_rev, "@X.Y.Z/00_config_dev.sql"],
             cwd=sandbox)
    assert ok.returncode == 0, "correct dev revision rejected"

    bad = run([sandbox / "check_config_fresh.sh", main_rev, "@X.Y.Z/00_config_dev.sql"],
              cwd=sandbox)
    assert bad.returncode != 0, "compared against the wrong local file"


# ---------------------------------------------------------------------------
# T0.9  fresh clone
# ---------------------------------------------------------------------------

def test_fresh_clone_has_a_template_but_no_working_config(tmp_path):
    """What a second person actually gets. The template must be present and the
    working copy absent, and init_config.sh must produce a usable config."""
    clone = tmp_path / "clone"
    r = run(["git", "clone", "--quiet", "--no-hardlinks", "--depth", "1",
             f"file://{REPO}", clone])
    if r.returncode != 0:
        pytest.skip(f"git clone unavailable: {r.stderr.strip()[:120]}")

    assert (clone / "scripts" / "00_config.sql.template").exists(), "template not in the clone"
    assert not (clone / "scripts" / "00_config.sql").exists(), "working config was committed"

    init = clone / "scripts" / "init_config.sh"
    os.chmod(init, os.stat(init).st_mode | stat.S_IEXEC)
    for helper in ("config_revision.sh", "check_config_fresh.sh", "publish_config.sh"):
        p = clone / "scripts" / helper
        if p.exists():
            os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)

    assert run([init], cwd=clone / "scripts").returncode == 0
    created = clone / "scripts" / "00_config.sql"
    assert created.exists()
    assert set_names(created) == set_names(clone / "scripts" / "00_config.sql.template")


# ---------------------------------------------------------------------------
# guard against the documentation going stale
# ---------------------------------------------------------------------------

def test_no_tracked_file_tells_operators_to_bump_config_revision():
    """CONFIG_REVISION is derived now. Any instruction to hand-bump it is wrong and
    would teach an operator to fight the tooling.

    Excludes tests/ (this file necessarily contains the phrase it searches for),
    the captured pre-extraction fixture, and the plans directory, which records the
    old behaviour deliberately as history.
    """
    r = run(["git", "-C", REPO, "grep", "-l", "-i", "bump CONFIG_REVISION", "--", ".",
             ":!tests", ":!.snowflake/cortex/plans"])
    offenders = [f for f in r.stdout.split() if f]
    assert not offenders, f"stale 'bump CONFIG_REVISION' instructions in: {offenders}"
