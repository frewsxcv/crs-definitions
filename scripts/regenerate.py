#!/usr/bin/env python3
"""Regenerate src/defs.rs, src/from_code.rs, and src/from_code_const.rs.

The bulk of the crate is mechanically derived from the spatial_ref_sys
table that ships with PostGIS. This script spins up a PostGIS container,
runs the same SQL used by PR #4 to dump every EPSG entry, and rewrites
the three generated files in src/.

Manual additions (currently just EPSG_3857_WEBMERC) are embedded in the
templates below so they survive regeneration. To add another manual
constant, extend DEFS_TAIL.

Usage:
    scripts/regenerate.py                    # rewrite the three files
    scripts/regenerate.py --check            # diff against working tree, exit 1 on drift
    scripts/regenerate.py --image IMAGE      # override the PostGIS image
    scripts/regenerate.py --skip-build       # skip the final `cargo check`

Requires `docker` on PATH. Uses only the Python stdlib.
"""

from __future__ import annotations

import argparse
import difflib
import shutil
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_IMAGE = "postgis/postgis:16-3.4"
CONTAINER_NAME = "crs-definitions-regen"
PG_PASSWORD = "regen"
READY_TIMEOUT_S = 60

SQL = (
    "SELECT 'EPSG_' || srid || '|' || srid || '|\"' || trim(proj4text) "
    "|| '\"|r#\"' || srtext || '\"#|' "
    "FROM spatial_ref_sys WHERE auth_name = 'EPSG' ORDER BY srid;"
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"

DEFS_PRELUDE = '''/// A CRS definition
#[derive(Debug, PartialEq)]
pub struct Def {
    /// EPSG code (e.g. `4326`)
    pub code: u16,
    #[cfg(feature = "proj4")]
    /// PROJ4 definition (e.g. `+proj=longlat +datum=WGS84 +no_defs`)
    pub proj4: &'static str,
    #[cfg(feature = "wkt")]
    /// Well-Known Text definition (e.g. `GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563,AUTHORITY["EPSG","7030"]],AUTHORITY["EPSG","6326"]],PRIMEM["Greenwich",0,AUTHORITY["EPSG","8901"]],UNIT["degree",0.0174532925199433,AUTHORITY["EPSG","9122"]],AUTHORITY["EPSG","4326"]]`)
    pub wkt: &'static str,
}

macro_rules! defs {
    ($(
        $name:ident|$code:literal|$proj4:literal|$wkt:literal|
    )*) => {
        $(
            pub const $name: Def = Def {
                code: $code,
                #[cfg(feature = "proj4")]
                proj4: $proj4,
                #[cfg(feature = "wkt")]
                wkt: $wkt,
            };
        )*
    }
}

#[rustfmt::skip]
defs![
'''

DEFS_TAIL = '''];

/// Alternative EPSG:3857 (WGS 84 / Pseudo-Mercator) definition that uses
/// the `webmerc` projection instead of `merc + nadgrids=@null`.
///
/// The default [`EPSG_3857`] mirrors PostGIS / proj4js, which represents
/// Web Mercator with `+proj=merc ... +nadgrids=@null`. When transforming
/// from a non-WGS84 datum, that form skips the source datum shift and can
/// produce results offset by 100m or more from PROJ.
///
/// Modern PROJ builds its EPSG:3857 pipeline around `+proj=webmerc` with
/// an explicit Helmert step, so this alternative matches PROJ's behaviour
/// at the cost of compatibility with proj4js (which does not support
/// `webmerc`). It is not returned by [`from_code`](crate::from_code) — use
/// it explicitly when PROJ-compatible accuracy matters.
///
/// See <https://github.com/frewsxcv/crs-definitions/issues/6>.
pub const EPSG_3857_WEBMERC: Def = Def {
    code: 3857,
    #[cfg(feature = "proj4")]
    proj4: "+proj=webmerc +ellps=WGS84 +lat_0=0 +lon_0=0 +x_0=0 +y_0=0 +towgs84=0,0,0,0,0,0,0",
    #[cfg(feature = "wkt")]
    wkt: EPSG_3857.wkt,
};
'''

FROM_CODE_PRELUDE = '''use crate::defs::*;

pub fn from_code(code: u16) -> Option<Def> {
    Some(match code {
'''

FROM_CODE_TAIL = '''        _ => return None,
    })
}
'''

FROM_CODE_CONST_PRELUDE = '''use crate::defs::*;

pub const fn from_code_const<const CODE: u16>() -> Def {
    match CODE {
'''

FROM_CODE_CONST_TAIL = '''        _ => panic!("Unknown EPSG code"),
    }
}
'''


def run(cmd: list[str], *, capture: bool = False, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=capture, text=True, check=check)


def remove_container() -> None:
    run(["docker", "rm", "-f", CONTAINER_NAME], capture=True, check=False)


def start_postgis(image: str) -> None:
    print(f"[regenerate] starting {image} as container {CONTAINER_NAME}", file=sys.stderr)
    remove_container()
    run([
        "docker", "run", "-d",
        "--name", CONTAINER_NAME,
        "-e", f"POSTGRES_PASSWORD={PG_PASSWORD}",
        image,
    ], capture=True)


def wait_until_ready() -> None:
    print("[regenerate] waiting for postgres to be ready", file=sys.stderr)
    deadline = time.monotonic() + READY_TIMEOUT_S
    while time.monotonic() < deadline:
        result = subprocess.run(
            ["docker", "exec", CONTAINER_NAME, "pg_isready", "-U", "postgres"],
            capture_output=True,
        )
        if result.returncode == 0:
            # spatial_ref_sys is populated by the postgis init scripts, which
            # may still be running even after pg_isready succeeds. Probe the
            # table directly until we get the expected row count.
            probe = subprocess.run(
                [
                    "docker", "exec", CONTAINER_NAME,
                    "psql", "-U", "postgres", "-d", "postgres",
                    "-At", "-c",
                    "SELECT count(*) FROM spatial_ref_sys WHERE auth_name = 'EPSG';",
                ],
                capture_output=True, text=True,
            )
            if probe.returncode == 0 and probe.stdout.strip().isdigit() and int(probe.stdout.strip()) > 0:
                return
        time.sleep(1)
    raise RuntimeError(f"postgis did not become ready within {READY_TIMEOUT_S}s")


def dump_defs_rows() -> list[str]:
    print("[regenerate] dumping EPSG rows from spatial_ref_sys", file=sys.stderr)
    result = run(
        [
            "docker", "exec", CONTAINER_NAME,
            "psql", "-U", "postgres", "-d", "postgres",
            "-At", "-c", SQL,
        ],
        capture=True,
    )
    rows = [line for line in result.stdout.splitlines() if line]
    if not rows:
        raise RuntimeError("SQL returned no rows")
    return rows


def code_from_row(row: str) -> int:
    # Row shape: EPSG_<srid>|<srid>|"<proj4>"|r#"<wkt>"#|
    return int(row.split("|", 2)[1])


def render_defs(rows: list[str]) -> str:
    return DEFS_PRELUDE + "\n".join(rows) + "\n" + DEFS_TAIL


def render_from_code(rows: list[str]) -> str:
    arms = "\n".join(f"        {code_from_row(r)} => EPSG_{code_from_row(r)}," for r in rows)
    return FROM_CODE_PRELUDE + arms + "\n" + FROM_CODE_TAIL


def render_from_code_const(rows: list[str]) -> str:
    arms = "\n".join(f"        {code_from_row(r)} => EPSG_{code_from_row(r)}," for r in rows)
    return FROM_CODE_CONST_PRELUDE + arms + "\n" + FROM_CODE_CONST_TAIL


def diff(path: Path, new_content: str) -> str:
    old = path.read_text() if path.exists() else ""
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new_content.splitlines(keepends=True),
            fromfile=str(path),
            tofile=f"{path} (regenerated)",
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--image", default=DEFAULT_IMAGE, help=f"PostGIS image (default: {DEFAULT_IMAGE})")
    parser.add_argument("--check", action="store_true", help="diff regenerated output against working tree, exit 1 on drift")
    parser.add_argument("--skip-build", action="store_true", help="skip the final `cargo check`")
    parser.add_argument("--keep-container", action="store_true", help="leave the postgis container running after the script exits")
    args = parser.parse_args()

    if not shutil.which("docker"):
        print("error: docker not found on PATH", file=sys.stderr)
        return 2

    try:
        start_postgis(args.image)
        wait_until_ready()
        rows = dump_defs_rows()
    finally:
        if not args.keep_container:
            remove_container()

    targets = {
        SRC / "defs.rs": render_defs(rows),
        SRC / "from_code.rs": render_from_code(rows),
        SRC / "from_code_const.rs": render_from_code_const(rows),
    }

    if args.check:
        diffs = [diff(p, c) for p, c in targets.items()]
        drift = [d for d in diffs if d]
        if drift:
            for d in drift:
                sys.stdout.write(d)
            print(f"[regenerate] {len(drift)} file(s) would change", file=sys.stderr)
            return 1
        print("[regenerate] working tree matches regenerated output", file=sys.stderr)
        return 0

    for path, content in targets.items():
        path.write_text(content)
        print(f"[regenerate] wrote {path.relative_to(REPO_ROOT)}", file=sys.stderr)

    if not args.skip_build:
        print("[regenerate] running `cargo check`", file=sys.stderr)
        run(["cargo", "check", "--manifest-path", str(REPO_ROOT / "Cargo.toml")])

    return 0


if __name__ == "__main__":
    sys.exit(main())
