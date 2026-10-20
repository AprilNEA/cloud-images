#!/usr/bin/env python3
"""Tests for scripts/pve-import-template.sh without a Proxmox VE node."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "pve-import-template.sh"


class PVEImportTest(unittest.TestCase):
    """Exercise the shell script through temporary mock executables."""

    @classmethod
    def setUpClass(cls) -> None:
        if os.geteuid() == 0:
            cls.runner_prefix: list[str] = []
            return

        unshare = shutil.which("unshare")
        if not unshare:
            raise unittest.SkipTest("The root-required script needs unshare or root.")

        probe = subprocess.run(
            [unshare, "--user", "--map-root-user", "--", "bash", "-c", "test \"$EUID\" -eq 0"],
            check=False,
            capture_output=True,
            text=True,
        )
        if probe.returncode != 0:
            raise unittest.SkipTest("User namespaces are unavailable for root-required tests.")
        cls.runner_prefix = [unshare, "--user", "--map-root-user", "--"]

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.temp = Path(self.tempdir.name)
        self.bin_dir = self.temp / "bin"
        self.bin_dir.mkdir()
        self.log_file = self.temp / "commands.jsonl"
        self.image = self.temp / "source.qcow2"
        self.image.touch()
        self._write_mocks()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _write_tool(self, name: str, source: str) -> None:
        path = self.bin_dir / name
        path.write_text(textwrap.dedent(source))
        path.chmod(0o755)

    def _write_mocks(self) -> None:
        self._write_tool(
            "qemu-img",
            """\
            #!/usr/bin/env python3
            import json
            import os
            import sys

            with open(os.environ["MOCK_LOG"], "a", encoding="utf-8") as handle:
                handle.write(json.dumps({"command": "qemu-img", "args": sys.argv[1:]}) + "\\n")
            if sys.argv[1:2] != ["info"]:
                raise SystemExit(90)
            print(os.environ.get("MOCK_QEMU_INFO", '{"format": "qcow2"}'))
            """,
        )
        self._write_tool(
            "qm",
            """\
            #!/usr/bin/env python3
            import json
            import os
            import sys

            args = sys.argv[1:]
            with open(os.environ["MOCK_LOG"], "a", encoding="utf-8") as handle:
                handle.write(json.dumps({"command": "qm", "args": args}) + "\\n")
            if (
                os.environ.get("MOCK_QM_FAIL_IMPORT") == "1"
                and args[:1] == ["set"]
                and "--scsi0" in args
            ):
                raise SystemExit(23)
            """,
        )
        self._write_tool(
            "pvesh",
            """\
            #!/usr/bin/env python3
            import json
            import os
            import sys

            args = sys.argv[1:]
            with open(os.environ["MOCK_LOG"], "a", encoding="utf-8") as handle:
                handle.write(json.dumps({"command": "pvesh", "args": args}) + "\\n")
            if len(args) < 2 or args[0] != "get":
                raise SystemExit(91)
            path = args[1]
            if path == "/cluster/nextid":
                raise SystemExit(1 if os.environ.get("MOCK_VM_EXISTS") == "1" else 0)
            if path.startswith("/storage/"):
                print(json.dumps({"content": os.environ.get("MOCK_STORAGE_CONTENT", "images")}))
                raise SystemExit(0)
            if path.startswith("/nodes/") and path.endswith("/status"):
                print(json.dumps({"active": os.environ.get("MOCK_STORAGE_ACTIVE", "1")}))
                raise SystemExit(0)
            raise SystemExit(92)
            """,
        )
        self._write_tool(
            "ip",
            """\
            #!/usr/bin/env python3
            import json
            import os
            import sys

            with open(os.environ["MOCK_LOG"], "a", encoding="utf-8") as handle:
                handle.write(json.dumps({"command": "ip", "args": sys.argv[1:]}) + "\\n")
            raise SystemExit(0 if os.environ.get("MOCK_BRIDGE_EXISTS", "1") == "1" else 1)
            """,
        )
        self._write_tool(
            "hostname",
            """\
            #!/usr/bin/env python3
            import json
            import os
            import sys

            with open(os.environ["MOCK_LOG"], "a", encoding="utf-8") as handle:
                handle.write(json.dumps({"command": "hostname", "args": sys.argv[1:]}) + "\\n")
            print("pve-test-node")
            """,
        )

    def run_script(self, arguments: list[str | Path], **settings: str) -> subprocess.CompletedProcess[str]:
        self.log_file.unlink(missing_ok=True)
        environment = os.environ.copy()
        environment.pop("TEMPLATE_NAME", None)
        environment.update(
            {
                "PATH": f"{self.bin_dir}{os.pathsep}{environment['PATH']}",
                "MOCK_LOG": str(self.log_file),
                "LC_ALL": "C",
            }
        )
        environment.update({key: str(value) for key, value in settings.items()})
        return subprocess.run(
            [*self.runner_prefix, "bash", str(SCRIPT), *(str(argument) for argument in arguments)],
            cwd=ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )

    def records(self, command: str | None = None) -> list[dict[str, object]]:
        if not self.log_file.exists():
            return []
        entries = [json.loads(line) for line in self.log_file.read_text().splitlines()]
        if command is None:
            return entries
        return [entry for entry in entries if entry["command"] == command]

    @staticmethod
    def option_value(arguments: list[str], option: str) -> str:
        return arguments[arguments.index(option) + 1]

    def test_valid_parameters_create_the_expected_template(self) -> None:
        result = self.run_script([self.image, "1234", "fast-lvm", "vmbr42"])

        self.assertEqual(result.returncode, 0, result.stderr)
        qm_calls = self.records("qm")
        self.assertEqual([call["args"][0] for call in qm_calls], ["create", "set", "set", "cloudinit", "template"])

        create = qm_calls[0]["args"]
        self.assertEqual(self.option_value(create, "--name"), "source")
        self.assertEqual(self.option_value(create, "--machine"), "q35")
        self.assertEqual(self.option_value(create, "--cpu"), "host")
        self.assertEqual(self.option_value(create, "--memory"), "1024")
        self.assertEqual(self.option_value(create, "--scsihw"), "virtio-scsi-single")
        self.assertEqual(self.option_value(create, "--scsi2"), "fast-lvm:cloudinit")
        self.assertEqual(self.option_value(create, "--serial0"), "socket")
        self.assertEqual(self.option_value(create, "--vga"), "serial0")
        self.assertEqual(self.option_value(create, "--net0"), "virtio,bridge=vmbr42")
        self.assertEqual(self.option_value(create, "--agent"), "1")
        self.assertEqual(self.option_value(create, "--ostype"), "l26")

        import_disk = qm_calls[1]["args"]
        self.assertEqual(
            self.option_value(import_disk, "--scsi0"),
            f"fast-lvm:0,import-from={self.image},discard=on,ssd=1",
        )
        self.assertNotIn("importdisk", [call["args"][0] for call in qm_calls])
        self.assertEqual(qm_calls[2]["args"], ["set", "1234", "--boot", "order=scsi0", "--ipconfig0", "ip=dhcp"])
        self.assertEqual(qm_calls[3]["args"], ["cloudinit", "dump", "1234", "user"])

    def test_existing_vmid_stops_before_qm_changes(self) -> None:
        result = self.run_script([self.image, "1234"], MOCK_VM_EXISTS="1")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already allocated or unavailable", result.stderr)
        self.assertEqual(self.records("qm"), [])

    def test_invalid_parameters_stop_before_image_inspection(self) -> None:
        for vm_id in ("0", "not-a-number", "1000000000", "0100", "999999999999999999999"):
            with self.subTest(vm_id=vm_id):
                result = self.run_script([self.image, vm_id])
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("VM ID", result.stderr)
                self.assertEqual(self.records("qemu-img"), [])
                self.assertEqual(self.records("qm"), [])

    def test_missing_image_stops_without_pve_commands(self) -> None:
        missing = self.temp / "missing.qcow2"
        result = self.run_script([missing])

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not exist", result.stderr)
        self.assertEqual(self.records(), [])

    def test_storage_without_images_content_stops_before_qm_changes(self) -> None:
        result = self.run_script([self.image], MOCK_STORAGE_CONTENT="iso,vztmpl")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not support images", result.stderr)
        self.assertEqual(self.records("qm"), [])

    def test_import_failure_does_not_convert_partial_vm_to_template(self) -> None:
        result = self.run_script([self.image], MOCK_QM_FAIL_IMPORT="1")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("did not remove any VM", result.stderr)
        qm_calls = self.records("qm")
        self.assertEqual([call["args"][0] for call in qm_calls], ["create", "set"])
        self.assertNotIn("template", [call["args"][0] for call in qm_calls])

    def test_default_name_does_not_use_debian_version_environment(self) -> None:
        named_image = self.temp / "stable-cloud.qcow2"
        named_image.touch()
        result = self.run_script([named_image], DEBIAN_VERSION="99")

        self.assertEqual(result.returncode, 0, result.stderr)
        create = self.records("qm")[0]["args"]
        self.assertEqual(self.option_value(create, "--name"), "stable-cloud")
        self.assertNotIn("99", self.option_value(create, "--name"))

    def test_image_path_with_spaces_is_preserved_in_import_from(self) -> None:
        spaced_image = self.temp / "cloud image with spaces.qcow2"
        spaced_image.touch()
        result = self.run_script([spaced_image])

        self.assertEqual(result.returncode, 0, result.stderr)
        import_disk = self.records("qm")[1]["args"]
        self.assertEqual(
            self.option_value(import_disk, "--scsi0"),
            f"local-lvm:0,import-from={spaced_image},discard=on,ssd=1",
        )

    def test_invalid_template_name_is_rejected(self) -> None:
        result = self.run_script([self.image], TEMPLATE_NAME="invalid name")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.records("qm"), [])

    def test_backing_file_is_rejected(self) -> None:
        result = self.run_script([self.image], MOCK_QEMU_INFO='{"format":"qcow2","backing-filename":"base.qcow2"}')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.records("qm"), [])

    def test_inactive_storage_is_rejected(self) -> None:
        result = self.run_script([self.image], MOCK_STORAGE_ACTIVE="0")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.records("qm"), [])

    def test_missing_bridge_is_rejected(self) -> None:
        result = self.run_script([self.image], MOCK_BRIDGE_EXISTS="0")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.records("qm"), [])


if __name__ == "__main__":
    unittest.main()
