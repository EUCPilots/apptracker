import contextlib
from email.message import Message
import http.client
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import validate_evergreen_api as api


RELEASE = {"Version": "1.2.3", "URI": "https://example.org/installer.exe", "Size": 12.5}
APP = {"Name": "TestApp", "Application": "Test App", "Link": "https://example.org/"}
HEALTH = {
    "status": "ok",
    "timestamp": "2026-11-08T03:00:00Z",
    "bindings": {"evergreen": True, "logsBucket": True},
}
INFO = {"message": "See the documentation", "documentation": "https://example.org/docs"}
ENDPOINTS = [{"Application": "TestApp", "Endpoints": ["example.org"]}]


def healthy_client(apps=None):
    responses = {
        "/": INFO, "/app": INFO, "/endpoints": INFO, "/health": HEALTH,
        "/endpoints/versions": ENDPOINTS, "/endpoints/downloads": ENDPOINTS,
        "/apps": apps if apps is not None else [APP],
    }
    return Mock(get=Mock(side_effect=lambda path: responses.get(path, [RELEASE])))


class Response(io.BytesIO):
    def __init__(self, value, status=200, content_type="application/json"):
        super().__init__(json.dumps(value).encode())
        self.code = status
        self.headers = Message()
        self.headers["Content-Type"] = content_type


class SchemaTests(unittest.TestCase):
    def test_all_routes_accept_valid_data(self):
        checked, failures = api.check_api(healthy_client())
        self.assertEqual(checked, 8)
        self.assertEqual(failures, [])

    def test_every_release_is_validated(self):
        with self.assertRaisesRegex(api.ValidationError, r"\$\[1\].URI"):
            api.validate_response("/app/TestApp", [RELEASE, {"Version": "2"}])

    def test_app_accepts_single_release_and_release_array(self):
        for value in (RELEASE, [RELEASE]):
            with self.subTest(value=value):
                api.validate_response("/app/TestApp", value)

    def test_null_empty_and_error_responses_fail(self):
        for path in ("/", "/app", "/endpoints", "/apps", "/health",
                     "/endpoints/versions", "/endpoints/downloads", "/app/TestApp"):
            for value in (None, [], {}, "", {"error": "upstream failed"}):
                with self.subTest(path=path, value=value):
                    with self.assertRaises(api.ValidationError):
                        api.validate_response(path, value)

    def test_nested_null_including_unknown_properties_fails(self):
        for value in ({**RELEASE, "Extra": None}, [{**RELEASE, "Extra": [None]}]):
            with self.assertRaisesRegex(api.ValidationError, "null value"):
                api.validate_response("/app/TestApp", value)

    def test_invalid_downloads_fail(self):
        for uri in ("", "not a URL", "ftp://example.org/file", "https://[invalid",
                    "https://example.org:wrong/file", "https://example.org/a b"):
            with self.subTest(uri=uri):
                with self.assertRaises(api.ValidationError):
                    api.validate_response("/app/TestApp", {**RELEASE, "URI": uri})

    def test_release_types_and_sentinel_fail(self):
        for change in ({"Version": 123}, {"Version": "RateLimited"}, {"Size": -1},
                       {"Size": True}, {"Size": float("inf")}, {"Architecture": False},
                       {"Type": ""}, {"Language": 1}):
            with self.subTest(change=change):
                with self.assertRaises(api.ValidationError):
                    api.validate_response("/app/TestApp", {**RELEASE, **change})

    def test_release_sizes_accept_numbers_and_evergreen_strings(self):
        for size in (0, 12, 12.5, "0", "311607296", "226623624 ", " 12.5 ",
                     "343.52 MB", "12MB", "1.5 GiB", "512 B", "2 kb"):
            for as_array in (False, True):
                with self.subTest(size=size, as_array=as_array):
                    release = {**RELEASE, "Size": size}
                    api.validate_response("/app/TestApp", [release] if as_array else release)
                    self.assertEqual(release["Size"], size)

    def test_invalid_release_sizes_fail(self):
        for size in (-1, float("inf"), float("-inf"), float("nan"), True, False,
                     None, [], {}, "", " ", "-1", "-1 MB", "NaN", "Infinity",
                     "1e309", "unknown", "12 widgets", "MB", "1.2.3 MB", "12 MB extra"):
            with self.subTest(size=size):
                with self.assertRaisesRegex(api.ValidationError, r"\$\[0\].Size:"):
                    api.validate_response("/app/TestApp", [{**RELEASE, "Size": size}])

    def test_app_identifiers_and_required_fields(self):
        for app in ({**APP, "Name": "../evil"}, {**APP, "Name": "A" * 65},
                    {**APP, "Application": ""}, {**APP, "Link": "broken"},
                    {"Name": "TestApp"}):
            with self.subTest(app=app):
                with self.assertRaises(api.ValidationError):
                    api.validate_response("/apps", [app])

    def test_duplicate_names_are_failure_but_queried_once(self):
        client = healthy_client([APP, {**APP, "Name": "testapp"}])
        checked, failures = api.check_api(client)
        self.assertEqual(checked, 8)
        self.assertEqual(len(failures), 1)
        self.assertIn("duplicate", failures[0][1])

    def test_malformed_discovery_still_checks_valid_app_names(self):
        client = healthy_client([None, APP, {"Name": "OtherApp"}])
        checked, failures = api.check_api(client)
        self.assertEqual(checked, 9)
        self.assertEqual(len(failures), 1)
        self.assertIn(unittest.mock.call("/app/OtherApp"), client.get.call_args_list)

    def test_failed_discovery_uses_local_catalog_without_reporting_success(self):
        client = healthy_client()
        original = client.get.side_effect

        def get(path):
            if path == "/apps":
                raise api.ValidationError("HTTP 500: unexpected status")
            return original(path)

        client.get.side_effect = get
        with patch.object(Path, "read_bytes", return_value=json.dumps([APP]).encode()):
            checked, failures = api.check_api(client)
        self.assertEqual(checked, 8)
        self.assertEqual(len(failures), 1)
        client.get.assert_any_call("/app/TestApp")

    def test_unreadable_fallback_is_explicit_failure(self):
        client = healthy_client([])
        with patch.object(Path, "read_bytes", side_effect=OSError):
            checked, failures = api.check_api(client)
        self.assertEqual(checked, 7)
        self.assertTrue(any("Fallback" in error for _, error in failures))
        self.assertTrue(any("coverage incomplete" in error for _, error in failures))

    def test_one_app_failure_does_not_skip_remaining_apps(self):
        client = healthy_client([APP, {**APP, "Name": "ZApp"}])
        original = client.get.side_effect

        def get(path):
            if path == "/app/TestApp":
                raise api.ValidationError("HTTP 429: unexpected status")
            return original(path)

        client.get.side_effect = get
        checked, failures = api.check_api(client)
        self.assertEqual(checked, 9)
        self.assertEqual(len(failures), 1)
        client.get.assert_any_call("/app/ZApp")

    def test_endpoint_hostnames_and_empty_lists_fail(self):
        for hosts in ([], None, ["https://example.org"], ["bad host"], [123], ["-bad.org"]):
            with self.subTest(hosts=hosts):
                with self.assertRaises(api.ValidationError):
                    api.validate_response("/endpoints/versions",
                                          [{"Application": "TestApp", "Endpoints": hosts}])

    def test_unhealthy_status_binding_and_timestamp_fail(self):
        for change in ({"status": "warning"}, {"status": "error"}, {"timestamp": "invalid"},
                       {"timestamp": "2026-11-08"},
                       {"bindings": {"evergreen": False, "logsBucket": True}},
                       {"bindings": {"evergreen": True, "logsBucket": 1}},
                       {"kvTest": {"accessible": False}},
                       {"kvTest": {"hasAllapps": False}},
                       {"kvTest": {"error": "KV error"}},
                       {"cache": {"memorySize": "1", "ttlSeconds": 10, "ttlHours": 1}}):
            with self.subTest(change=change):
                with self.assertRaises(api.ValidationError):
                    api.validate_response("/health", {**HEALTH, **change})

    def test_valid_optional_health_fields(self):
        api.validate_response("/health", {
            **HEALTH, "environment": "production",
            "cache": {"memorySize": 0, "ttlSeconds": 43200, "ttlHours": 12,
                      "memoryKeys": [], "cleared": False},
            "kvTest": {"accessible": True, "hasAllapps": True,
                       "allappsType": "object", "allappsLength": 1},
        })

    def test_non_json_and_nonfinite_numbers_fail(self):
        for raw in (b"not JSON", b"\xff", b'{"Size": NaN}', b'{"Size": Infinity}'):
            with self.subTest(raw=raw):
                with self.assertRaises(api.ValidationError):
                    api.decode_json(raw)


class RequestTests(unittest.TestCase):
    @patch.object(api.urllib.request, "urlopen")
    def test_secret_user_agent_is_used_and_not_retained_as_response(self, urlopen):
        response = Response([RELEASE])
        urlopen.return_value = response
        client = api.ApiClient("Test/1 (Example)")
        self.assertEqual(client.get("/app/TestApp"), [RELEASE])
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_header("User-agent"), "Test/1 (Example)")
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 15)
        self.assertTrue(response.closed)
        self.assertEqual(set(vars(client)), {"user_agent", "last_throttled_request"})

    @patch.object(api.urllib.request, "urlopen")
    def test_information_routes_accept_documented_error_statuses(self, urlopen):
        for path, status in (("/", 404), ("/app", 400), ("/endpoints", 404)):
            with self.subTest(path=path):
                response = Response(INFO, status)
                urlopen.side_effect = urllib.error.HTTPError(
                    api.API_URL + path, status, "error", response.headers, response
                )
                value = api.ApiClient("Test/1").get(path)
                api.validate_response(path, value)
                self.assertTrue(response.closed)

    @patch.object(api.urllib.request, "urlopen")
    def test_unexpected_http_status_is_failure_without_body(self, urlopen):
        for status in (400, 404, 429, 500):
            with self.subTest(status=status):
                response = Response({"secret": "never print this"}, status)
                urlopen.side_effect = urllib.error.HTTPError(
                    api.API_URL, status, "secret error message", response.headers, response
                )
                with self.assertRaisesRegex(api.ValidationError, f"^HTTP {status}: unexpected status$"):
                    api.ApiClient("Test/1").get("/app/TestApp")
                self.assertTrue(response.closed)

    @patch.object(api.urllib.request, "urlopen")
    def test_wrong_content_type_fails(self, urlopen):
        urlopen.return_value = Response(RELEASE, content_type="text/html")
        with self.assertRaisesRegex(api.ValidationError, "content type"):
            api.ApiClient("Test/1").get("/app/TestApp")

    @patch.object(api.urllib.request, "urlopen")
    def test_transport_failures_are_safe_diagnostics(self, urlopen):
        for error in (urllib.error.URLError("secret"), TimeoutError("secret"),
                      http.client.RemoteDisconnected("secret"), OSError("secret")):
            with self.subTest(error=error):
                urlopen.side_effect = error
                with self.assertRaises(api.ValidationError) as result:
                    api.ApiClient("Test/1").get("/apps")
                self.assertNotIn("secret", str(result.exception))

    def test_all_throttled_requests_including_repeats_wait_ten_seconds(self):
        clock = [0.0]
        starts = []

        def sleep(delay):
            self.assertGreaterEqual(delay, 0)
            clock[0] += delay

        def urlopen(*args, **kwargs):
            starts.append(clock[0])
            clock[0] += 0.25
            return Response([RELEASE])

        paths = ["/app/MicrosoftEdgeWebView2Runtime", "/app/MicrosoftEdge",
                 "/app/AdobeAcrobatReaderDC", "/app/CitrixWorkspaceApp",
                 "/app/MicrosoftEdge"]
        with patch.object(api.time, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(api.time, "sleep", side_effect=sleep), \
                patch.object(api.urllib.request, "urlopen", side_effect=urlopen):
            client = api.ApiClient("Test/1")
            for path in paths:
                client.get(path)
        self.assertEqual(len(starts), 5)
        for previous, current in zip(starts, starts[1:]):
            self.assertGreaterEqual(current - previous, 10)

    def test_throttled_failure_is_not_retried_and_next_attempt_waits(self):
        with patch.object(api.time, "monotonic", return_value=0), \
                patch.object(api.time, "sleep") as sleep, \
                patch.object(api.urllib.request, "urlopen", side_effect=TimeoutError) as urlopen:
            client = api.ApiClient("Test/1")
            for _ in range(2):
                with self.assertRaises(api.ValidationError):
                    client.get("/app/MicrosoftEdge")
            sleep.assert_called_once_with(10)
            self.assertEqual(urlopen.call_count, 2)

    def test_non_throttled_requests_do_not_sleep(self):
        with patch.object(api.time, "sleep") as sleep, \
                patch.object(api.urllib.request, "urlopen", side_effect=lambda *a, **kw: Response(INFO)):
            client = api.ApiClient("Test/1")
            client.get("/")
            client.get("/")
            sleep.assert_not_called()


def managed_issue(number=12, state="open"):
    return {"number": number, "state": state, "title": api.TITLE, "body": api.MARKER}


class IssueTests(unittest.TestCase):
    def sync(self, issues, failures=None):
        github = Mock()
        github.issues.return_value = iter(issues)
        api.sync_issue(github, 8, failures or [], "https://example.org/run",
                       "2026-11-08T03:00:00Z")
        return github

    def test_failure_creates_one_issue_with_all_errors(self):
        github = self.sync([], [("/apps", "null value"), ("/app/TestApp", "HTTP 500")])
        github.request.assert_called_once()
        resource, method, body = github.request.call_args.args
        self.assertEqual((resource, method), ("issues", "POST"))
        self.assertIn("/apps: null value", body["body"])
        self.assertIn("/app/TestApp: HTTP 500", body["body"])
        self.assertIn(api.MARKER, body["body"])

    def test_repeated_failure_updates_existing_issue(self):
        github = self.sync([managed_issue()], [("/health", "API unhealthy")])
        self.assertEqual(github.request.call_args.args[:2], ("issues/12", "PATCH"))
        self.assertEqual(github.request.call_args.args[2]["state"], "open")
        self.assertEqual(github.request.call_count, 1)

    def test_recovery_comments_and_closes(self):
        github = self.sync([managed_issue()])
        self.assertEqual(github.request.call_args_list[0].args[:2], ("issues/12/comments", "POST"))
        self.assertEqual(github.request.call_args_list[1].args,
                         ("issues/12", "PATCH", {"state": "closed", "state_reason": "completed"}))

    def test_later_failure_reopens_same_issue(self):
        github = self.sync([managed_issue(state="closed")], [("/apps", "null value")])
        self.assertEqual(github.request.call_args.args[:2], ("issues/12", "PATCH"))
        self.assertEqual(github.request.call_args.args[2]["state"], "open")

    def test_failure_repetition_recovery_and_regression_keep_one_issue(self):
        issues = []
        comments = []

        def request(resource, method, body):
            if resource == "issues":
                issues.append({"number": 12, "state": "open", **body})
            elif resource.endswith("/comments"):
                comments.append(body)
            else:
                self.assertEqual((resource, method), ("issues/12", "PATCH"))
                issues[0].update(body)

        github = Mock(request=Mock(side_effect=request))
        github.issues.side_effect = lambda: iter(issues)
        for failures, state in (
            ([("/apps", "null")], "open"),
            ([("/health", "unhealthy")], "open"),
            ([], "closed"),
            ([], "closed"),
            ([("/app/TestApp", "HTTP 500")], "open"),
        ):
            api.sync_issue(github, 8, failures, "run", "now")
            self.assertEqual(len(issues), 1)
            self.assertEqual(issues[0]["state"], state)
        self.assertEqual(len(comments), 1)
        self.assertEqual(sum(call.args[0] == "issues"
                             for call in github.request.call_args_list), 1)

    def test_success_with_no_open_issue_does_not_write(self):
        for issues in ([], [managed_issue(state="closed")]):
            with self.subTest(issues=issues):
                self.sync(issues).request.assert_not_called()

    def test_unrelated_issues_are_not_modified(self):
        unrelated = {"number": 1, "title": "Other problem", "body": "Details", "state": "open"}
        github = self.sync([unrelated], [("/apps", "null value")])
        self.assertEqual(github.request.call_args.args[:2], ("issues", "POST"))

    def test_multiple_open_managed_issues_fail_without_creating(self):
        github = Mock()
        github.issues.return_value = [managed_issue(), managed_issue(13)]
        with self.assertRaisesRegex(RuntimeError, "Multiple open"):
            api.sync_issue(github, 8, [("/apps", "null")], "run", "now")
        github.request.assert_not_called()

    def test_marker_survives_title_change_and_title_survives_marker_removal(self):
        for change in ({"title": "Renamed"}, {"body": ""}):
            with self.subTest(change=change):
                github = self.sync([{**managed_issue(), **change}], [("/apps", "null")])
                self.assertEqual(github.request.call_args.args[:2], ("issues/12", "PATCH"))

    def test_open_issue_preferred_over_closed_history(self):
        github = self.sync([managed_issue(), managed_issue(13, "closed")], [("/apps", "null")])
        self.assertEqual(github.request.call_args.args[0], "issues/12")

    def test_errors_are_escaped_and_body_is_bounded(self):
        github = self.sync([], [("/apps", "<script>" + "x" * 70000)])
        body = github.request.call_args.args[2]["body"]
        self.assertNotIn("<script>", body)
        self.assertIn("&lt;script&gt;", body)
        self.assertLess(len(body), 65536)

    def test_pagination_and_pull_request_exclusion(self):
        with patch.dict(os.environ, {"GITHUB_TOKEN": "test", "GITHUB_REPOSITORY": "owner/repo"}):
            github = api.GitHubClient()
        first = [{"number": i, "pull_request": {}} for i in range(100)]
        github.request = Mock(side_effect=[first, [managed_issue()]])
        self.assertEqual(list(github.issues()), [managed_issue()])
        self.assertEqual(github.request.call_count, 2)
        self.assertIn("page=2", github.request.call_args.args[0])

    @patch.object(api.urllib.request, "urlopen", side_effect=urllib.error.URLError("private"))
    def test_github_errors_fail_explicitly(self, urlopen):
        with patch.dict(os.environ, {"GITHUB_TOKEN": "test", "GITHUB_REPOSITORY": "owner/repo"}):
            github = api.GitHubClient()
        with self.assertRaisesRegex(RuntimeError, "GitHub issue reconciliation failed"):
            github.request("issues")


class MainTests(unittest.TestCase):
    def environment(self):
        return {
            "API_TEST_USERAGENT": "Test/1 (Example)", "GITHUB_TOKEN": "test-token",
            "GITHUB_REPOSITORY": "owner/repo", "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1",
        }

    def test_success_logs_and_summary_contain_counts_only(self):
        with tempfile.TemporaryDirectory() as directory:
            summary = Path(directory) / "summary"
            env = {**self.environment(), "GITHUB_STEP_SUMMARY": str(summary)}
            stdout = io.StringIO()
            with patch.dict(os.environ, env, clear=True), \
                    patch.object(api, "ApiClient", return_value=healthy_client()), \
                    patch.object(api, "GitHubClient") as github, \
                    contextlib.redirect_stdout(stdout):
                github.return_value.issues.return_value = []
                self.assertEqual(api.main(), 0)
            expected = "Evergreen API: 8 endpoint checks, 0 failures.\n"
            self.assertEqual(stdout.getvalue(), expected)
            self.assertEqual(summary.read_text(), expected)
            self.assertEqual(list(Path(directory).iterdir()), [summary])
            github.return_value.request.assert_not_called()

    def test_missing_or_invalid_useragent_creates_configuration_issue(self):
        for agent in ("", "a", "invalid\nagent", "invalid\u00e9agent", "a" * 101):
            with self.subTest(agent=agent):
                env = {**self.environment(), "API_TEST_USERAGENT": agent}
                with patch.dict(os.environ, env, clear=True), \
                        patch.object(api, "ApiClient") as client, \
                        patch.object(api, "GitHubClient") as github, \
                        contextlib.redirect_stdout(io.StringIO()):
                    github.return_value.issues.return_value = []
                    self.assertEqual(api.main(), 1)
                    client.assert_not_called()
                    self.assertIn("API_TEST_USERAGENT", github.return_value.request.call_args.args[2]["body"])

    def test_failure_logs_and_issues_redact_secrets(self):
        env = self.environment()
        diagnostics = f"null field: {env['API_TEST_USERAGENT']} {env['GITHUB_TOKEN']}"
        stdout = io.StringIO()
        with patch.dict(os.environ, env, clear=True), \
                patch.object(api, "check_api", return_value=(
                    8, [(f"/app/{env['GITHUB_TOKEN']}", diagnostics)]
                )), \
                patch.object(api, "GitHubClient") as github, \
                contextlib.redirect_stdout(stdout):
            github.return_value.issues.return_value = []
            self.assertEqual(api.main(), 1)
            body = github.return_value.request.call_args.args[2]["body"]
        for secret in (env["API_TEST_USERAGENT"], env["GITHUB_TOKEN"]):
            self.assertNotIn(secret, stdout.getvalue())
            self.assertNotIn(secret, body)

    def test_reconciliation_error_returns_failure(self):
        with patch.dict(os.environ, self.environment(), clear=True), \
                patch.object(api, "check_api", return_value=(8, [])), \
                patch.object(api, "sync_issue", side_effect=RuntimeError("GitHub reconciliation failed")), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(api.main(), 1)


if __name__ == "__main__":
    unittest.main()
