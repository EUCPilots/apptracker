"""Map Evergreen applications to equivalent winget packages.

Reads the Evergreen application list and application JSON from this repository,
reads the public winget source indexes, and writes a JSON mapping of each
Evergreen application to zero or more winget package identifiers. Winget
packages that are published as one package per processor architecture are
grouped and matched to the single Evergreen application.
"""

import argparse
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import zipfile


WINGET_CDN = "https://cdn.winget.microsoft.com/cache"
SCHEMA_VERSION = 1
USER_AGENT = "EUCPilots-apptracker-winget-mapping"
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_JSON = REPO_ROOT / "json"
DEFAULT_OUTPUT = REPO_ROOT / "winget" / "evergreen-winget.json"
DEFAULT_OVERRIDES = Path(__file__).resolve().parent / "WingetOverrides.json"

ARCHITECTURES = {
    "x64": "x64", "amd64": "x64", "x8664": "x64", "win64": "x64", "64bit": "x64",
    "x86": "x86", "win32": "x86", "32bit": "x86", "ia32": "x86", "i386": "x86", "i686": "x86",
    "arm64": "arm64", "aarch64": "arm64",
    "arm": "arm",
    "neutral": "neutral", "any": "neutral",
}
ARCH_SUFFIX = re.compile(r"[-_ ]?\(?(x64|amd64|x86_64|win64|64-?bit|x86|win32|32-?bit|ia32|arm64|aarch64)\)?$", re.I)
ARCH_WORDS = re.compile(r"\(?\b(x64|amd64|x86_64|win64|64-?bit|x86|win32|32-?bit|ia32|arm64|aarch64)\b\)?", re.I)

# Hosts that serve many unrelated applications: matching only the host is not evidence.
SHARED_HOSTS = {
    "github.com", "gitlab.com", "bitbucket.org", "codeberg.org", "sourceforge.net",
    "sourceforge.io", "githubusercontent.com", "amazonaws.com", "cloudfront.net",
    "azureedge.net", "windows.net", "googleapis.com", "akamaized.net", "akamaihd.net",
    "microsoft.com", "aka.ms", "google.com", "apple.com", "npmjs.org", "pypi.org",
    "nuget.org", "visualstudio.com", "digitaloceanspaces.com", "b-cdn.net",
    "fosshub.com", "softpedia.com", "chocolatey.org", "r2.dev", "jfrog.io",
}
PATH_HOSTS = {"github.com": 2, "gitlab.com": 2, "bitbucket.org": 2, "codeberg.org": 2}
SECOND_LEVEL = {"co", "com", "net", "org", "gov", "edu", "ac", "or", "ne"}

NAME_EXACT = 100
NAME_PUBLISHER = 90
NAME_TOKENS = 60
NAME_PARTIAL = 40
INSTALLER_URL = 40
HOMEPAGE_URL = 20
PARTIAL_CANDIDATES = 10
FILLER_WORDS = {"app", "apps", "and", "browser", "client", "desktop", "for", "free", "the", "software", "inc"}


def log(message):
    print(message, file=sys.stderr, flush=True)


def normalize(value):
    value = unicodedata.normalize("NFKD", str(value or "")).lower().replace("+", "plus")
    return re.sub(r"[^a-z0-9]", "", value)


def split_words(value):
    """Split CamelCase, digits, and punctuation into lowercase tokens."""
    value = unicodedata.normalize("NFKD", str(value or "")).replace("+", " plus ")
    value = re.sub(r"([a-z])([A-Z])", r"\1 \2", value)
    value = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", value)
    value = re.sub(r"([A-Za-z])([0-9])|([0-9])([A-Za-z])", lambda m: " ".join(filter(None, m.groups())), value)
    return [token for token in re.split(r"[^a-z0-9]+", value.lower()) if token]


def canonical_architecture(value):
    return ARCHITECTURES.get(normalize(value))


def split_architecture(package_id):
    """Return the package ID without its architecture qualifier, and that architecture."""
    segments = package_id.split(".")
    for index in range(len(segments) - 1, 1, -1):
        arch = canonical_architecture(segments[index])
        if arch and arch != "neutral":
            return ".".join(segments[:index] + segments[index + 1:]), arch
    match = ARCH_SUFFIX.search(segments[-1]) if len(segments) > 1 else None
    if match and match.start() > 0 and match.group(0)[0] in "-_ (":
        segments[-1] = segments[-1][:match.start()]
        return ".".join(segments), canonical_architecture(match.group(1))
    return package_id, None


def strip_architecture_words(value):
    return ARCH_WORDS.sub(" ", value or "")


def url_keys(url):
    """Return comparable site keys for a URL; shared hosts are qualified by path."""
    try:
        parts = urllib.parse.urlsplit(str(url).strip())
        host = (parts.hostname or "").lower()
    except ValueError:
        return set()
    if not host:
        return set()
    host = host[4:] if host.startswith("www.") else host
    labels = host.split(".")
    if len(labels) >= 3 and labels[-2] in SECOND_LEVEL and len(labels[-1]) == 2:
        domain = ".".join(labels[-3:])
    else:
        domain = ".".join(labels[-2:])
    path = [segment.lower() for segment in parts.path.split("/") if segment]
    if domain in PATH_HOSTS and host == domain:
        depth = PATH_HOSTS[domain]
        return {f"{domain}/{'/'.join(path[:depth])}"} if len(path) >= depth else set()
    if domain == "sourceforge.net" and len(path) >= 2 and path[0] in ("project", "projects"):
        return {f"sourceforge.net/{path[1]}"}
    if domain in SHARED_HOSTS:
        return set()
    return {domain}


def as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


# --- Evergreen ---------------------------------------------------------------

def read_json(path):
    with open(path, encoding="utf-8-sig") as handle:
        return json.load(handle)


def load_evergreen(json_path):
    apps = []
    for item in as_list(read_json(Path(json_path) / "supported-apps.json")):
        name = item.get("Name")
        if not name:
            continue
        architectures, urls, versions = set(), set(), []
        app_file = Path(json_path) / f"{name}.json"
        if app_file.is_file():
            try:
                releases = as_list(read_json(app_file))
            except (OSError, ValueError) as error:
                log(f"Unable to read {app_file.name}: {error}")
                releases = []
            for release in releases:
                if not isinstance(release, dict):
                    continue
                if release.get("Version") and str(release["Version"]) not in versions:
                    versions.append(str(release["Version"]))
                arch = canonical_architecture(release.get("Architecture"))
                if arch:
                    architectures.add(arch)
                for key in ("URI", "Uri", "URL", "Url"):
                    if isinstance(release.get(key), str):
                        urls.add(release[key])
        apps.append({
            "name": name,
            "application": item.get("Application") or name,
            "link": item.get("Link"),
            "architectures": sorted(architectures),
            "urls": sorted(urls),
            "versions": versions,
        })
    return sorted(apps, key=lambda app: app["name"].lower())


# --- winget -------------------------------------------------------------------

def download(url, destination, timeout=120, attempts=3):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response, open(destination, "wb") as handle:
                shutil.copyfileobj(response, handle)
            return destination
        except (urllib.error.URLError, OSError) as error:
            if attempt == attempts:
                raise
            log(f"Retrying {url}: {error}")
            time.sleep(2 * attempt)


def extract_index(msix, destination):
    with zipfile.ZipFile(msix) as archive:
        with archive.open("Public/index.db") as source, open(destination, "wb") as handle:
            shutil.copyfileobj(source, handle)
    return destination


def fetch_indexes(work_dir):
    """Download and extract the v2 (package list) and v1 (manifest paths) indexes."""
    indexes = {}
    for key, file_name in (("v2", "source2.msix"), ("v1", "source.msix")):
        db = Path(work_dir) / f"index-{key}.db"
        if not db.is_file():
            msix = Path(work_dir) / file_name
            log(f"Downloading {WINGET_CDN}/{file_name}")
            download(f"{WINGET_CDN}/{file_name}", msix)
            extract_index(msix, db)
            msix.unlink()
        indexes[key] = db
    return indexes


def load_packages(v2_db):
    with sqlite3.connect(f"file:{v2_db}?mode=ro", uri=True) as connection:
        publishers = {}
        for publisher, package in connection.execute("SELECT norm_publisher, package FROM norm_publishers2"):
            publishers.setdefault(package, set()).add(publisher)
        tags = {}
        for tag, package in connection.execute(
                "SELECT t.tag, m.package FROM tags2_map m JOIN tags2 t ON t.rowid = m.tag"):
            tags.setdefault(package, set()).add(tag)
        packages = []
        for rowid, package_id, name, moniker, version in connection.execute(
                "SELECT rowid, id, name, moniker, latest_version FROM packages"):
            packages.append({
                "id": package_id,
                "name": name,
                "moniker": moniker,
                "version": version,
                "publishers": sorted(publishers.get(rowid, ())),
                "tags": sorted(tags.get(rowid, ())),
            })
    return packages


def manifest_paths(v1_db, packages):
    """Return the CDN path of the merged manifest for each package's latest version."""
    wanted = {package["id"]: package["version"] for package in packages}
    paths = {}
    query = """
        WITH RECURSIVE path(rowid, parent, value) AS (
            SELECT rowid, parent, pathpart FROM pathparts WHERE rowid = ?
            UNION ALL
            SELECT p.rowid, p.parent, p.pathpart || '/' || path.value
            FROM pathparts p JOIN path ON p.rowid = path.parent)
        SELECT value FROM path WHERE parent IS NULL"""
    with sqlite3.connect(f"file:{v1_db}?mode=ro", uri=True) as connection:
        rows = connection.execute(
            "SELECT i.id, v.version, m.pathpart, m.rowid FROM manifest m "
            "JOIN ids i ON i.rowid = m.id JOIN versions v ON v.rowid = m.version "
            f"WHERE i.id IN ({','.join('?' * len(wanted))})", list(wanted))
        best = {}
        for package_id, version, pathpart, rowid in rows:
            rank = (version == wanted[package_id], rowid)
            if package_id not in best or rank > best[package_id][0]:
                best[package_id] = (rank, pathpart)
        for package_id, (_, pathpart) in best.items():
            row = connection.execute(query, (pathpart,)).fetchone()
            if row:
                paths[package_id] = row[0]
    return paths


def unquote(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        value = value[1:-1]
    return value


def parse_manifest(text):
    """Extract the fields used for matching from a winget merged manifest (YAML)."""
    result = {"InstallerUrls": [], "Architectures": []}
    in_installers = False
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        top = re.match(r"^([A-Za-z]+):\s*(.*)$", line)
        if top:
            key, value = top.groups()
            in_installers = key == "Installers"
            if key in ("PackageName", "Publisher", "PackageUrl", "PublisherUrl") and value:
                result[key] = unquote(value)
            elif key == "Architecture" and value:
                result["Architectures"].append(unquote(value))
            continue
        if in_installers:
            item = re.match(r"^(?:- |  )([A-Za-z]+):\s*(.*)$", line)
            if item and item.group(2):
                key, value = item.group(1), unquote(item.group(2))
                if key == "Architecture":
                    result["Architectures"].append(value)
                elif key == "InstallerUrl":
                    result["InstallerUrls"].append(value)
    architectures = {canonical_architecture(value) for value in result["Architectures"]}
    result["Architectures"] = sorted(arch for arch in architectures if arch)
    result["InstallerUrls"] = sorted(set(result["InstallerUrls"]))
    return result


def fetch_manifests(paths, cache_dir=None, workers=16):
    def fetch(item):
        package_id, path = item
        cache = None
        if cache_dir:
            cache = Path(cache_dir) / "manifests" / hashlib.sha256(path.encode()).hexdigest()
            if cache.is_file():
                return package_id, parse_manifest(cache.read_text(encoding="utf-8"))
        request = urllib.request.Request(f"{WINGET_CDN}/{urllib.parse.quote(path)}", headers={"User-Agent": USER_AGENT})
        for attempt in range(1, 4):
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    text = response.read().decode("utf-8", "replace")
                break
            except (urllib.error.URLError, OSError) as error:
                if attempt == 3:
                    log(f"Unable to fetch manifest for {package_id}: {error}")
                    return package_id, None
                time.sleep(attempt)
        if cache:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(text, encoding="utf-8")
        return package_id, parse_manifest(text)

    manifests = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        for package_id, manifest in executor.map(fetch, sorted(paths.items())):
            if manifest is not None:
                manifests[package_id] = manifest
    return manifests


# --- Matching -----------------------------------------------------------------

def group_packages(packages):
    """Group winget packages that differ only by an architecture qualifier in the ID."""
    groups = {}
    for package in packages:
        base_id, arch = split_architecture(package["id"])
        group = groups.setdefault(base_id.lower(), {"base": base_id, "packages": []})
        group["packages"].append(dict(package, idArchitecture=arch))
    for group in groups.values():
        group["packages"].sort(key=lambda package: package["id"].lower())
        primary = min(group["packages"], key=lambda package: (package["idArchitecture"] is not None, len(package["id"])))
        segments = group["base"].split(".")
        name = strip_architecture_words(primary["name"])
        group["keys"] = {
            "id": normalize(group["base"]),
            "tail": normalize("".join(segments[1:])) if len(segments) > 1 else None,
            "name": normalize(name),
            "publisherName": {normalize(segments[0]) + normalize(name)}
            | {publisher + normalize(name) for publisher in primary["publishers"]},
        }
        group["tokens"] = set(split_words(group["base"])) | set(split_words(name))
        group["productTokens"] = set(split_words(".".join(segments[1:])))
        group["text"] = normalize(group["base"]) + " " + " ".join(sorted(group["tokens"]))
    return groups


def build_lookup(groups):
    lookup = {}
    for key, group in groups.items():
        values = [group["keys"]["id"], group["keys"]["name"], group["keys"]["tail"], *group["keys"]["publisherName"]]
        for value in values:
            if value:
                lookup.setdefault(value, set()).add(key)
    return lookup


def app_token_sets(app):
    sets = [split_words(app["name"]), split_words(app["application"])]
    return [tokens for tokens in sets if tokens]


def tokens_match(tokens, group):
    """Every Evergreen token must appear in the winget ID or name; numbers must match exactly."""
    numbers = {token for token in group["tokens"] if token.isdigit()}
    for token in tokens:
        if token.isdigit():
            if token not in numbers:
                return False
        elif not any(token in candidate for candidate in group["tokens"] if not candidate.isdigit()):
            return False
    return True


def partial_match(app, group):
    """Return True when an Evergreen name partially matches a winget package.

    Publisher tokens (the first ID segment) are not evidence unless the product is named after
    the publisher (e.g. Brave.Brave), numbers must not conflict, every product token in the
    winget ID must be found in the Evergreen name, and at least half of the remaining
    Evergreen tokens must be found in the winget package.
    """
    publisher = set(split_words(group["base"].split(".")[0]))
    product = {token for token in group["productTokens"] if len(token) > 1 or token.isdigit()}
    product_is_publisher = product <= publisher
    required = product if product_is_publisher else product - publisher
    if not required:
        return False
    numbers = {token for token in group["tokens"] if token.isdigit()}
    words = [token for token in group["tokens"] if not token.isdigit()]

    def found(token, pool):
        return token in pool or (not token.isdigit() and len(token) >= 3 and any(
            token in word or (len(word) >= 3 and word in token) for word in pool if not word.isdigit()))

    for tokens in app_token_sets(app):
        app_tokens = set(tokens) - FILLER_WORDS
        app_numbers = {token for token in app_tokens if token.isdigit()}
        if numbers - app_numbers or app_numbers - numbers - publisher:
            continue
        considered = app_tokens if product_is_publisher else app_tokens - publisher
        if not considered:
            continue
        coverage = sum(1 for token in considered if found(token, words) or token in numbers) / len(considered)
        if coverage >= 0.5 and all(any(token in word for word in app_tokens) for token in required):
            return True
    return False


def name_score(app, group):
    keys = group["keys"]
    app_keys = {normalize(app["name"]), normalize(app["application"])}
    if keys["id"] in app_keys:
        return NAME_EXACT
    if app_keys & keys["publisherName"]:
        return NAME_PUBLISHER
    if keys["name"] in app_keys or (keys["tail"] and keys["tail"] in app_keys):
        return NAME_TOKENS + 10
    if any(tokens_match(tokens, group) for tokens in app_token_sets(app)):
        return NAME_TOKENS
    if partial_match(app, group):
        return NAME_PARTIAL
    return 0


def name_candidates(app, groups, lookup, token_index):
    keys = set()
    for value in (normalize(app["name"]), normalize(app["application"])):
        keys |= lookup.get(value, set())
    for tokens in app_token_sets(app):
        words = [token for token in tokens if not token.isdigit()]
        if not words:
            continue
        rarest = min(words, key=lambda token: len(token_index["all"].get(token, ())))
        pool = token_index["all"].get(rarest) or {key for key, group in groups.items() if rarest in group["text"]}
        keys |= {key for key in pool if tokens_match(tokens, groups[key])}
    partial = set()
    for token in {token for tokens in app_token_sets(app) for token in tokens} - FILLER_WORDS:
        if len(token) >= 3 and not token.isdigit():
            partial |= token_index["product"].get(token, set())
    ranked = sorted((len(groups[key]["keys"]["id"]), key) for key in partial - keys if partial_match(app, groups[key]))
    keys |= {key for _, key in ranked[:PARTIAL_CANDIDATES]}
    return keys


def build_token_index(groups):
    index = {"all": {}, "product": {}}
    for key, group in groups.items():
        for token in group["tokens"]:
            index["all"].setdefault(token, set()).add(key)
        for token in group["productTokens"]:
            index["product"].setdefault(token, set()).add(key)
    return index


def url_score(app, group, manifests):
    app_installers = set().union(*(url_keys(url) for url in app["urls"])) if app["urls"] else set()
    app_home = url_keys(app["link"]) if app["link"] else set()
    winget_installers, winget_home = set(), set()
    for package in group["packages"]:
        manifest = manifests.get(package["id"]) or {}
        for url in manifest.get("InstallerUrls", []):
            winget_installers |= url_keys(url)
        for key in ("PackageUrl", "PublisherUrl"):
            if manifest.get(key):
                winget_home |= url_keys(manifest[key])
    if app_installers & winget_installers:
        return INSTALLER_URL + (HOMEPAGE_URL if app_home & winget_home else 0)
    if (app_home | app_installers) & (winget_home | winget_installers):
        return HOMEPAGE_URL
    return 0


def version_parts(value):
    return re.findall(r"\d+", str(value or ""))


def version_similarity(app, group):
    """Number of leading version components shared by the Evergreen and winget latest versions."""
    best = 0
    for app_version in app.get("versions", [])[:5]:
        app_parts = version_parts(app_version)
        for package in group["packages"]:
            shared = 0
            for left, right in zip(app_parts, version_parts(package.get("version"))):
                if left.lstrip("0") != right.lstrip("0"):
                    break
                shared += 1
            best = max(best, shared)
    return best


def candidate_rank(app, key, groups, manifests):
    group = groups[key]
    names = name_score(app, group)
    urls = url_score(app, group, manifests)
    extra = abs(len(group["keys"]["id"]) - len(normalize(app["name"])))
    app_text = normalize(app["name"]) + " " + normalize(app["application"])
    unmatched = sum(1 for token in group["productTokens"] if not token.isdigit() and token not in app_text)
    latest = max((tuple(int(part) for part in version_parts(package.get("version"))[:4]) for package in group["packages"]),
                 default=())
    return {"key": key, "name": names, "url": urls, "unmatched": unmatched,
            "version": version_similarity(app, group), "latest": latest, "extra": extra}


def accept(rank):
    if rank["name"] >= NAME_PUBLISHER:
        return True
    if rank["name"] >= NAME_TOKENS:
        return rank["url"] > 0
    return rank["name"] >= NAME_PARTIAL and rank["url"] >= INSTALLER_URL


def confidence(rank):
    if rank["url"] >= INSTALLER_URL and rank["name"] >= NAME_TOKENS:
        return "high"
    if rank["name"] >= NAME_PUBLISHER and rank["url"] > 0:
        return "high"
    return "medium"


def rank_candidates(app, candidates, groups, manifests):
    ranks = [dict(candidate_rank(app, key, groups, manifests)) for key in candidates]
    for rank in ranks:
        rank["accepted"] = accept(rank)
    return sorted(ranks, key=rank_order)


def choose(app, candidates, groups, manifests):
    """Return the best-ranked accepted winget package group for an Evergreen app.

    A group contains every per-architecture winget package for the same application.
    """
    return next((rank for rank in rank_candidates(app, candidates, groups, manifests) if rank["accepted"]), None)


def rank_order(rank):
    """Sort by name strength, extra product words (e.g. .EXE, .LTS), version agreement, URL evidence, then newest."""
    tier = 4 if rank["name"] >= NAME_EXACT else 3 if rank["name"] >= NAME_PUBLISHER else 2 if rank["name"] >= NAME_TOKENS else 1
    newest = tuple(-part for part in rank["latest"])
    return (-tier, rank["unmatched"], -rank["version"], -rank["url"], -rank["name"], rank["extra"], newest, rank["key"])


def package_entry(package, manifests):
    manifest = manifests.get(package["id"]) or {}
    architectures = manifest.get("Architectures") or []
    if package["idArchitecture"]:
        architectures = [package["idArchitecture"]]
    return {
        "id": package["id"],
        "name": manifest.get("PackageName") or package["name"],
        "publisher": manifest.get("Publisher"),
        "architectures": sorted(set(architectures)),
    }


def architecture_map(app_architectures, entries):
    mapping = {}
    for arch in app_architectures or []:
        if arch == "neutral":
            continue
        ids = [entry["id"] for entry in entries
               if arch in entry["architectures"] or not entry["architectures"] or "neutral" in entry["architectures"]]
        if ids:
            mapping[arch] = ids
    return mapping


def build_mapping(apps, packages, manifests, overrides, groups=None):
    groups = groups or group_packages(packages)
    group_of = {package["id"].lower(): key for key, group in groups.items() for package in group["packages"]}
    lookup = build_lookup(groups)
    token_index = build_token_index(groups)
    applications = {}
    for app in apps:
        if app["name"] in overrides:
            chosen_packages = {}
            for package_id in overrides[app["name"]]:
                key = group_of.get(package_id.lower())
                if key is None:
                    log(f"Override for {app['name']}: winget package {package_id} not found")
                    continue
                for package in groups[key]["packages"]:
                    chosen_packages[package["id"]] = package
            chosen_packages = list(chosen_packages.values())
            match = "override" if chosen_packages or not overrides[app["name"]] else None
        else:
            chosen = choose(app, name_candidates(app, groups, lookup, token_index), groups, manifests)
            chosen_packages = groups[chosen["key"]]["packages"] if chosen else []
            match = confidence(chosen) if chosen else None
        entries = [package_entry(package, manifests) for package in chosen_packages]
        entries.sort(key=lambda entry: entry["id"].lower())
        applications[app["name"]] = {
            "application": app["application"],
            "architectures": app["architectures"],
            "match": match,
            "winget": entries,
            "wingetByArchitecture": architecture_map(app["architectures"], entries),
        }
    return {
        "schemaVersion": SCHEMA_VERSION,
        "source": WINGET_CDN,
        "summary": {
            "applications": len(applications),
            "matched": sum(1 for item in applications.values() if item["winget"]),
            "unmatched": sum(1 for item in applications.values() if not item["winget"]),
        },
        "applications": applications,
    }


def load_overrides(path):
    if not path or not Path(path).is_file():
        return {}
    data = read_json(path)
    if not isinstance(data, dict) or not all(
            isinstance(value, list) and all(isinstance(item, str) for item in value) for value in data.values()):
        raise ValueError(f"{path}: expected an object mapping Evergreen app names to arrays of winget IDs")
    return data


def write_if_changed(path, data):
    content = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    path = Path(path)
    if path.is_file() and path.read_text(encoding="utf-8") == content:
        log(f"No changes to {path}")
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8", newline="\n")
    os.replace(temporary, path)
    log(f"Wrote {path}")
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json-path", default=DEFAULT_JSON, help="Directory containing supported-apps.json and app JSON")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help="Mapping JSON file to write")
    parser.add_argument("--overrides", default=DEFAULT_OVERRIDES, help="JSON file of manual mappings")
    parser.add_argument("--cache-dir", help="Reuse downloaded indexes and manifests from this directory")
    parser.add_argument("--explain", action="append", default=[], metavar="APP",
                        help="Print candidate rankings for an Evergreen app (repeatable)")
    args = parser.parse_args(argv)

    apps = load_evergreen(args.json_path)
    if not apps:
        raise SystemExit("No Evergreen applications found.")
    overrides = load_overrides(args.overrides)

    work_dir = args.cache_dir or tempfile.mkdtemp(prefix="winget-")
    Path(work_dir).mkdir(parents=True, exist_ok=True)
    try:
        indexes = fetch_indexes(work_dir)
        packages = load_packages(indexes["v2"])
        log(f"Loaded {len(apps)} Evergreen apps and {len(packages)} winget packages")

        groups = group_packages(packages)
        lookup, token_index = build_lookup(groups), build_token_index(groups)
        candidate_ids = set()
        for app in apps:
            for key in name_candidates(app, groups, lookup, token_index):
                candidate_ids.update(package["id"] for package in groups[key]["packages"])
        group_of = {package["id"].lower(): key for key, group in groups.items() for package in group["packages"]}
        for package_ids in overrides.values():
            for package_id in package_ids:
                key = group_of.get(package_id.lower())
                if key:
                    candidate_ids.update(package["id"] for package in groups[key]["packages"])
        candidates = [package for package in packages if package["id"] in candidate_ids]
        paths = manifest_paths(indexes["v1"], candidates)
        log(f"Fetching {len(paths)} winget manifests for candidate packages")
        manifests = fetch_manifests(paths, args.cache_dir)
        if paths and len(manifests) < len(paths) * 0.9:
            raise SystemExit(f"Fetched only {len(manifests)} of {len(paths)} manifests; mapping not updated.")

        mapping = build_mapping(apps, packages, manifests, overrides, groups)
        for app in (app for app in apps if app["name"].lower() in {name.lower() for name in args.explain}):
            log(f"Candidates for {app['name']}:")
            for rank in rank_candidates(app, name_candidates(app, groups, lookup, token_index), groups, manifests):
                log(f"  {groups[rank['key']]['base']}: {rank}")
    finally:
        if not args.cache_dir:
            shutil.rmtree(work_dir, ignore_errors=True)

    summary = mapping["summary"]
    log(f"Matched {summary['matched']} of {summary['applications']} Evergreen apps to winget packages")
    write_if_changed(args.output, mapping)
    return 0


if __name__ == "__main__":
    sys.exit(main())
