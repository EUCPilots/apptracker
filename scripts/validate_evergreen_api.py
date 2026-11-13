"""Validate Evergreen API 1.6.0 without persisting successful responses."""

import datetime
import html
import http.client
import json
import math
import os
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


API_URL = "https://evergreen-api.stealthpuppy.com"
MARKER = "<!-- apptracker:evergreen-api-validation -->"
TITLE = "Evergreen API validation failed"
THROTTLED = {
    "/app/microsoftedgewebview2runtime",
    "/app/microsoftedge",
    "/app/adobeacrobatreaderdc",
    "/app/citrixworkspaceapp",
}
KNOWN_NULL_VALUES = {
    "/app/jetbrainsspace": frozenset({"$[0].Build"}),
    "/app/microsoftopenjdk11": frozenset({"$[0].Checksum"}),
    "/app/microsoftopenjdk17": frozenset({"$[0].Checksum"}),
    "/app/microsoftopenjdk21": frozenset({"$[0].Checksum"}),
    "/app/microsoftopenjdk25": frozenset({"$[0].Checksum"}),
    "/app/mozillafirefox": frozenset({"$[515].Sha256"}),
    "/app/mozillathunderbird": frozenset({"$[264].Sha256"}),
    "/app/oraclejava17": frozenset({"$[0].Sha256"}),
    "/app/oraclejava20": frozenset({"$[0].Sha256"}),
    "/app/oraclejava21": frozenset({"$[0].Sha256"}),
    "/app/oraclejava22": frozenset({"$[0].Sha256"}),
    "/app/oraclejava23": frozenset({"$[0].Sha256"}),
    "/app/oraclejava26": frozenset({"$[0].Sha256"}),
    "/app/redhatopenjdk17": frozenset({"$[0].Checksum"}),
    "/app/redhatopenjdk21": frozenset({"$[0].Checksum"}),
    "/app/redhatopenjdk8": frozenset({"$[0].Checksum"}),
}
APP_ID = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


class ValidationError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise ValidationError(message)


def non_null(value, path="$", allowed_null_paths=frozenset()):
    require(value is not None or path in allowed_null_paths, f"{path}: null value")
    if isinstance(value, dict):
        for key, item in value.items():
            non_null(item, f"{path}.{key}", allowed_null_paths)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            non_null(item, f"{path}[{index}]", allowed_null_paths)


def text(value, path):
    require(isinstance(value, str) and bool(value.strip()), f"{path}: expected nonempty string")


def uri(value, path):
    text(value, path)
    try:
        parsed = urllib.parse.urlsplit(value)
        hostname = parsed.hostname
        parsed.port
    except ValueError as error:
        raise ValidationError(f"{path}: malformed URI") from error
    require(
        parsed.scheme in ("http", "https") and bool(hostname)
        and not any(char.isspace() for char in value),
        f"{path}: expected HTTP(S) URI",
    )


def fields(value, required, path="$"):
    require(isinstance(value, dict) and bool(value), f"{path}: expected nonempty object")
    for key in required:
        require(key in value, f"{path}.{key}: missing required field")
    return value


def records(value):
    require(isinstance(value, list) and bool(value), "$: expected nonempty array")
    for index, item in enumerate(value):
        require(isinstance(item, dict), f"$[{index}]: expected object")
    return value


def validate_response(path, value):
    non_null(value, allowed_null_paths=KNOWN_NULL_VALUES.get(path.lower(), frozenset()))
    if path in ("/", "/app", "/endpoints"):
        fields(value, ("message",))
        text(value["message"], "$.message")
        if path == "/endpoints":
            require("documentation" in value, "$.documentation: missing required field")
        if "documentation" in value:
            uri(value["documentation"], "$.documentation")
    elif path == "/apps":
        names = set()
        for index, app in enumerate(records(value)):
            location = f"$[{index}]"
            fields(app, ("Name", "Application", "Link"), location)
            text(app["Name"], f"{location}.Name")
            require(APP_ID.fullmatch(app["Name"]), f"{location}.Name: invalid application identifier")
            require(app["Name"].lower() not in names, f"{location}.Name: duplicate application identifier")
            names.add(app["Name"].lower())
            text(app["Application"], f"{location}.Application")
            uri(app["Link"], f"{location}.Link")
    elif path.startswith("/app/"):
        # The published schema describes one release; Evergreen also returns release arrays.
        releases = records(value) if isinstance(value, list) else [fields(value, ("Version", "URI"))]
        for index, release in enumerate(releases):
            location = f"$[{index}]"
            fields(release, ("Version", "URI"), location)
            text(release["Version"], f"{location}.Version")
            require(release["Version"].lower() not in ("ratelimited", "error", "null"),
                    f"{location}.Version: error sentinel")
            uri(release["URI"], f"{location}.URI")
            # Evergreen sizes include numbers, numeric strings, and strings with units.
            if "Size" in release:
                size = release["Size"]
                require((type(size) in (int, float)
                         and (type(size) is int or math.isfinite(size)) and size >= 0)
                        or (isinstance(size, str) and re.fullmatch(
                            r"[0-9]+(?:\.[0-9]+)?(?:\s*(?:[KMGTPE]i?)?B)?",
                            size.strip(), re.IGNORECASE)),
                        f"{location}.Size: expected nonnegative number or size string")
            for key in ("Architecture", "Type", "Language"):
                if key in release:
                    text(release[key], f"{location}.{key}")
    elif path in ("/endpoints/versions", "/endpoints/downloads"):
        for index, endpoint in enumerate(records(value)):
            location = f"$[{index}]"
            fields(endpoint, ("Application", "Endpoints"), location)
            text(endpoint["Application"], f"{location}.Application")
            hosts = endpoint["Endpoints"]
            require(isinstance(hosts, list) and bool(hosts),
                    f"{location}.Endpoints: expected nonempty array")
            for host_index, host in enumerate(hosts):
                require(isinstance(host, str) and len(host) <= 253 and all(
                    re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                    for label in host.rstrip(".").split(".")
                ), f"{location}.Endpoints[{host_index}]: expected hostname")
    elif path == "/health":
        fields(value, ("status", "timestamp", "bindings"))
        require(value["status"] == "ok", "$.status: API is not healthy")
        text(value["timestamp"], "$.timestamp")
        try:
            timestamp = datetime.datetime.fromisoformat(value["timestamp"].replace("Z", "+00:00"))
        except ValueError as error:
            raise ValidationError("$.timestamp: expected ISO 8601 date-time") from error
        require(timestamp.tzinfo is not None, "$.timestamp: timezone required")
        bindings = fields(value["bindings"], ("evergreen", "logsBucket"), "$.bindings")
        for key in ("evergreen", "logsBucket"):
            require(bindings[key] is True, f"$.bindings.{key}: binding unavailable")
        if "environment" in value:
            text(value["environment"], "$.environment")
        if "cache" in value:
            cache = fields(value["cache"], ("memorySize", "ttlSeconds", "ttlHours"), "$.cache")
            for key in ("memorySize", "ttlSeconds", "ttlHours"):
                require(type(cache[key]) in (int, float)
                        and (type(cache[key]) is int or math.isfinite(cache[key]))
                        and cache[key] >= 0,
                        f"$.cache.{key}: expected nonnegative number")
            for key in ("memorySize", "ttlSeconds"):
                require(type(cache[key]) is int, f"$.cache.{key}: expected integer")
            if "memoryKeys" in cache:
                require(isinstance(cache["memoryKeys"], list), "$.cache.memoryKeys: expected array")
                for item in cache["memoryKeys"]:
                    text(item, "$.cache.memoryKeys[]")
            if "cleared" in cache:
                require(type(cache["cleared"]) is bool, "$.cache.cleared: expected boolean")
        if "kvTest" in value:
            kv = fields(value["kvTest"], (), "$.kvTest")
            for key in ("accessible", "hasAllapps"):
                if key in kv:
                    require(kv[key] is True, f"$.kvTest.{key}: KV check failed")
            require("error" not in kv, "$.kvTest.error: KV check failed")
            for key in ("allappsType", "rawDataPreview"):
                if key in kv:
                    text(kv[key], f"$.kvTest.{key}")
            if "allappsLength" in kv:
                require(type(kv["allappsLength"]) is int and kv["allappsLength"] > 0,
                        "$.kvTest.allappsLength: expected positive integer")
    else:
        raise ValidationError("No validator for endpoint")


def decode_json(raw):
    def reject_constant(_value):
        raise ValidationError("Response contains a non-finite JSON number")

    try:
        return json.loads(raw, parse_constant=reject_constant)
    except (ValueError, UnicodeDecodeError) as error:
        raise ValidationError("Response is not valid JSON") from error


class ApiClient:
    def __init__(self, user_agent):
        self.user_agent = user_agent
        self.last_throttled_request = None

    def get(self, path):
        throttled = path.lower() in THROTTLED
        if throttled and self.last_throttled_request is not None:
            delay = 10 - (time.monotonic() - self.last_throttled_request)
            if delay > 0:
                time.sleep(delay)
        request = urllib.request.Request(
            API_URL + path, headers={"User-Agent": self.user_agent, "Accept": "application/json"}
        )
        try:
            try:
                response = urllib.request.urlopen(request, timeout=15)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                allowed = {200}
                # These informational routes intentionally return errors in API 1.6.0.
                allowed.update({"/": {404}, "/app": {400}, "/endpoints": {404}}.get(path, set()))
                require(response.code in allowed, f"HTTP {response.code}: unexpected status")
                require(response.headers.get_content_type() == "application/json",
                        "Expected application/json content type")
                return decode_json(response.read())
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as error:
            # Do not include transport messages or bodies which could expose secrets.
            raise ValidationError(f"Request failed ({type(error).__name__})") from error
        finally:
            if throttled:
                self.last_throttled_request = time.monotonic()


def check_api(client):
    failures = []
    checked = 0
    apps = None
    for path in ("/", "/app", "/endpoints", "/health", "/endpoints/versions",
                 "/endpoints/downloads", "/apps"):
        checked += 1
        try:
            value = client.get(path)
            if path == "/apps":
                apps = value
            validate_response(path, value)
        except ValidationError as error:
            failures.append((path, str(error)))
        finally:
            value = None

    if not isinstance(apps, list) or not apps:
        # Discovery failure must not prevent attempts against known applications.
        try:
            apps = decode_json((Path(__file__).resolve().parent.parent
                                / "json" / "supported-apps.json").read_bytes())
            validate_response("/apps", apps)
        except (OSError, ValidationError) as error:
            failures.append(("/apps", f"Fallback app discovery failed ({type(error).__name__})"))
            apps = []
    names = sorted({app["Name"] for app in apps if isinstance(app, dict)
                    and isinstance(app.get("Name"), str) and APP_ID.fullmatch(app["Name"])},
                   key=str.lower)
    if not names:
        failures.append(("/apps", "No application identifiers available; app coverage incomplete"))
    seen = set()
    for name in names:
        if name.lower() in seen:
            continue
        seen.add(name.lower())
        path = f"/app/{urllib.parse.quote(name, safe='')}"
        checked += 1
        try:
            validate_response(path, client.get(path))
        except ValidationError as error:
            failures.append((path, str(error)))
    return checked, failures


class GitHubClient:
    def __init__(self):
        self.token = os.environ["GITHUB_TOKEN"]
        self.base = (os.environ.get("GITHUB_API_URL", "https://api.github.com")
                     + "/repos/" + os.environ["GITHUB_REPOSITORY"])

    def request(self, resource, method="GET", body=None):
        request = urllib.request.Request(
            self.base + "/" + resource,
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
                "User-Agent": "AppTracker-API-Validation",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return decode_json(response.read())
        except urllib.error.HTTPError as error:
            status = error.code
            error.close()
            raise RuntimeError(f"GitHub issue reconciliation failed: HTTP {status}") from None
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException,
                ValidationError) as error:
            raise RuntimeError(f"GitHub issue reconciliation failed ({type(error).__name__})") from None

    def issues(self):
        page = 1
        while True:
            issues = self.request(f"issues?state=all&per_page=100&page={page}")
            yield from (issue for issue in issues if "pull_request" not in issue)
            if len(issues) < 100:
                break
            page += 1


def sync_issue(github, checked, failures, run_url, observed_at):
    managed = [issue for issue in github.issues()
               if MARKER in (issue.get("body") or "") or issue["title"] == TITLE]
    opened = [issue for issue in managed if issue["state"] == "open"]
    if len(opened) > 1:
        raise RuntimeError("Multiple open API validation issues exist; consolidate before rerunning.")
    issue = opened[0] if opened else max(managed, key=lambda item: item["number"], default=None)
    if not failures:
        if issue and issue["state"] == "open":
            github.request(f"issues/{issue['number']}/comments", "POST", {
                "body": f"All {checked} endpoint checks passed at {observed_at}. "
                        f"Recovered in [workflow run]({run_url}).",
            })
            github.request(f"issues/{issue['number']}", "PATCH",
                           {"state": "closed", "state_reason": "completed"})
        return
    details = "\n".join(f"{path}: {error}" for path, error in failures)
    # GitHub issue bodies are limited to 65,536 characters.
    escaped = html.escape(details)
    if len(escaped) > 55000:
        escaped = escaped[:55000] + "\n[Further errors omitted; see workflow logs.]"
    body = (
        f"{MARKER}\n## Evergreen API validation failed\n\n"
        f"Observed: {observed_at}\n\n"
        f"Failed checks: {len(failures)}; endpoint requests checked: {checked}.\n\n"
        f"<pre>{escaped}</pre>\n\n[Workflow run and logs]({run_url})\n\n"
        "Repeated failures update this issue. A fully successful run closes it; "
        "a later failure reopens it. Successful response payloads are not stored."
    )
    if issue:
        github.request(f"issues/{issue['number']}", "PATCH", {"state": "open", "body": body})
    else:
        github.request("issues", "POST", {"title": TITLE, "body": body})


def redact(message, secrets):
    for secret in secrets:
        if secret:
            message = message.replace(secret, "[REDACTED]")
    return message


def main():
    for name in ("GITHUB_TOKEN", "GITHUB_REPOSITORY", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT"):
        if not os.environ.get(name):
            print(f"Required environment variable {name} is missing.", file=sys.stderr)
            return 1
    agent = os.environ.get("API_TEST_USERAGENT", "")
    if not 3 <= len(agent) <= 100 or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9\-._/:;(), \t]*[A-Za-z0-9)]", agent
    ):
        checked, failures = 0, [("configuration", "API_TEST_USERAGENT is missing or invalid")]
    else:
        checked, failures = check_api(ApiClient(agent))
    secrets = (agent, os.environ["GITHUB_TOKEN"])
    failures = [(redact(path, secrets), redact(error, secrets)) for path, error in failures]
    # Only counts and failure diagnostics leave memory, never successful payloads.
    summary = f"Evergreen API: {checked} endpoint checks, {len(failures)} failures."
    print(summary)
    for path, error in failures:
        print(f"{path}: {error}")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as output:
            output.write(summary + "\n")
    run_url = (f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/"
               f"{os.environ['GITHUB_REPOSITORY']}/actions/runs/"
               f"{os.environ['GITHUB_RUN_ID']}/attempts/{os.environ['GITHUB_RUN_ATTEMPT']}")
    try:
        sync_issue(GitHubClient(), checked, failures, run_url,
                   datetime.datetime.now(datetime.timezone.utc).isoformat())
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        return 1
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
