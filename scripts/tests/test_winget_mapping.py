from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import map_evergreen_winget as wm


def package(package_id, name, version="1.0", publishers=()):
    return {"id": package_id, "name": name, "moniker": None, "version": version,
            "publishers": list(publishers), "tags": []}


def app(name, application, urls=(), link=None, architectures=(), versions=()):
    return {"name": name, "application": application, "link": link,
            "urls": list(urls), "architectures": list(architectures), "versions": list(versions)}


def manifest(urls, architectures=(), home=None):
    return {"InstallerUrls": list(urls), "Architectures": list(architectures), "PackageUrl": home}


def mapping(apps, packages, manifests=None, overrides=None):
    with unittest.mock.patch.object(wm, "log"):
        return wm.build_mapping(apps, packages, manifests or {}, overrides or {})["applications"]


class ArchitectureTests(unittest.TestCase):
    def test_split_architecture_from_package_id(self):
        cases = {
            "Microsoft.VCRedist.2015+.x64": ("Microsoft.VCRedist.2015+", "x64"),
            "Adobe.Acrobat.Reader.32-bit": ("Adobe.Acrobat.Reader", "x86"),
            "Postman.Postman.arm64": ("Postman.Postman", "arm64"),
            "binjr.core-x64": ("binjr.core", "x64"),
            "Microsoft.Arm": ("Microsoft.Arm", None),
            "7zip.7zip": ("7zip.7zip", None),
        }
        for package_id, expected in cases.items():
            with self.subTest(package_id=package_id):
                self.assertEqual(wm.split_architecture(package_id), expected)

    def test_parse_manifest_reads_installers_only_from_installers_block(self):
        text = """Installers:
- Architecture: x64
  InstallerUrl: https://example.org/app-x64.exe
  NestedInstallerFiles:
  - RelativeFilePath: app.exe
- Architecture: arm64
  InstallerUrl: 'https://example.org/app-arm64.exe'
Localization:
- PackageUrl: https://example.org/de
PackageName: Example
PackageUrl: https://example.org/
"""
        result = wm.parse_manifest(text)
        self.assertEqual(result["Architectures"], ["arm64", "x64"])
        self.assertEqual(result["InstallerUrls"], ["https://example.org/app-arm64.exe", "https://example.org/app-x64.exe"])
        self.assertEqual(result["PackageUrl"], "https://example.org/")


class UrlKeyTests(unittest.TestCase):
    def test_shared_hosts_are_qualified_by_project(self):
        self.assertEqual(wm.url_keys("https://github.com/ip7z/7zip/releases/download/x.exe"), {"github.com/ip7z/7zip"})
        self.assertEqual(wm.url_keys("https://downloads.sourceforge.net/project/keepass/x.exe"), {"sourceforge.net/keepass"})
        self.assertEqual(wm.url_keys("https://download.microsoft.com/x.exe"), set())
        self.assertEqual(wm.url_keys("https://www.example.co.uk/x"), {"example.co.uk"})


class MatchingTests(unittest.TestCase):
    def test_multi_architecture_app_maps_to_each_architecture_package(self):
        packages = [
            package("Microsoft.VCRedist.2015+.x64", "Microsoft Visual C++ 2015-2022 Redistributable (x64)"),
            package("Microsoft.VCRedist.2015+.x86", "Microsoft Visual C++ 2015-2022 Redistributable (x86)"),
            package("Microsoft.VCRedist.2015+.arm64", "Microsoft Visual C++ 2015-2022 Redistributable (Arm64)"),
            package("Microsoft.VCRedist.2013.x64", "Microsoft Visual C++ 2013 Redistributable (x64)"),
        ]
        result = mapping([app("MicrosoftVCRedist2015+", "Microsoft VCRedist 2015+", architectures=["arm64", "x64", "x86"])],
                         packages)["MicrosoftVCRedist2015+"]
        self.assertEqual([entry["id"] for entry in result["winget"]], [
            "Microsoft.VCRedist.2015+.arm64", "Microsoft.VCRedist.2015+.x64", "Microsoft.VCRedist.2015+.x86"])
        self.assertEqual(result["wingetByArchitecture"], {
            "arm64": ["Microsoft.VCRedist.2015+.arm64"],
            "x64": ["Microsoft.VCRedist.2015+.x64"],
            "x86": ["Microsoft.VCRedist.2015+.x86"],
        })

    def test_unsuffixed_package_uses_manifest_architectures(self):
        packages = [package("Postman.Postman", "Postman"), package("Postman.Postman.arm64", "Postman")]
        urls = ["https://dl.pstmn.io/download/latest/win64"]
        manifests = {"Postman.Postman": manifest(urls, ["x64"]), "Postman.Postman.arm64": manifest(urls, ["arm64"])}
        result = mapping([app("Postman", "Postman", urls=urls, architectures=["arm64", "x64"])], packages, manifests)["Postman"]
        self.assertEqual(result["wingetByArchitecture"], {"arm64": ["Postman.Postman.arm64"], "x64": ["Postman.Postman"]})

    def test_partial_name_requires_installer_url_evidence(self):
        packages = [package("Brave.Brave", "Brave")]
        evergreen = app("BraveBrowser", "Brave Software Brave Browser", urls=["https://github.com/brave/brave-browser/x.exe"])
        self.assertEqual(mapping([evergreen], packages)["BraveBrowser"]["winget"], [])
        manifests = {"Brave.Brave": manifest(["https://github.com/brave/brave-browser/y.exe"])}
        self.assertEqual([entry["id"] for entry in mapping([evergreen], packages, manifests)["BraveBrowser"]["winget"]],
                         ["Brave.Brave"])

    def test_partial_name_rejects_sibling_products_and_conflicting_numbers(self):
        urls = ["https://example.org/installer.exe"]
        packages = [package("ESET.EndpointAntivirus", "ESET Endpoint Antivirus"),
                    package("Amazon.Corretto.8.JDK", "Amazon Corretto 8")]
        manifests = {key: manifest(urls) for key in ("ESET.EndpointAntivirus", "Amazon.Corretto.8.JDK")}
        result = mapping([app("ESETEndpointEncryption", "ESET Endpoint Encryption", urls=urls),
                          app("AmazonCorretto15", "Amazon Corretto 15", urls=urls)], packages, manifests)
        self.assertEqual(result["ESETEndpointEncryption"]["winget"], [])
        self.assertEqual(result["AmazonCorretto15"]["winget"], [])

    def test_prefers_package_without_extra_words_then_matching_version(self):
        urls = ["https://zoom.example/x.exe"]
        packages = [package("Zoom.Zoom", "Zoom", "6.0"), package("Zoom.Zoom.EXE", "Zoom", "6.1"),
                    package("Vendor.Tool.3", "Tool", "3.2"), package("Vendor.Tool.4", "Tool", "4.1")]
        manifests = {item["id"]: manifest(urls) for item in packages}
        result = mapping([app("Zoom", "Zoom", urls=urls, versions=["6.1.0"]),
                          app("VendorTool", "Vendor Tool", urls=urls, versions=["4.1.2"])], packages, manifests)
        self.assertEqual([entry["id"] for entry in result["Zoom"]["winget"]], ["Zoom.Zoom"])
        self.assertEqual([entry["id"] for entry in result["VendorTool"]["winget"]], ["Vendor.Tool.4"])

    def test_overrides_include_architecture_siblings_and_can_clear_a_match(self):
        packages = [package("Vendor.App.x64", "App (x64)"), package("Vendor.App.x86", "App (x86)"),
                    package("Other.App", "App")]
        result = mapping([app("VendorApp", "Vendor App"), app("App", "App")], packages,
                         overrides={"VendorApp": ["vendor.app.x64"], "App": []})
        self.assertEqual([entry["id"] for entry in result["VendorApp"]["winget"]], ["Vendor.App.x64", "Vendor.App.x86"])
        self.assertEqual(result["VendorApp"]["match"], "override")
        self.assertEqual((result["App"]["winget"], result["App"]["match"]), ([], "override"))


class OutputTests(unittest.TestCase):
    def test_write_if_changed_only_rewrites_changed_content(self):
        with tempfile.TemporaryDirectory() as directory, unittest.mock.patch.object(wm, "log"):
            path = Path(directory) / "out" / "mapping.json"
            self.assertTrue(wm.write_if_changed(path, {"a": 1}))
            self.assertFalse(wm.write_if_changed(path, {"a": 1}))
            self.assertTrue(wm.write_if_changed(path, {"a": 2}))
            self.assertEqual(path.read_text(encoding="utf-8"), '{\n  "a": 2\n}\n')


if __name__ == "__main__":
    unittest.main()
