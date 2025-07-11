# enrichment_modules/sysprep/analyzer.py
import tempfile
import textwrap
from pathlib import Path

import structlog
import yara_x
from common.models import EnrichmentResult, FileObject, Finding, FindingCategory, FindingOrigin, Transform
from common.state_helpers import get_file_enriched
from common.storage import StorageMinio

from file_enrichment_modules.module_loader import EnrichmentModule

logger = structlog.get_logger(module=__name__)

# Port of https://github.com/NetSPI/PowerHuntShares/blob/46238ba37dc85f65f2c1d7960f551ea3d80c236a/Scripts/ConfigParsers/parser-sysprep.inf.ps1
#   Original Author: Scott Sutherland, NetSPI (@_nullbind / nullbind)
#   License: BSD 3-clause


class SysprepParser(EnrichmentModule):
    def __init__(self):
        super().__init__("sysprep_parser")
        self.storage = StorageMinio()
        # the workflows this module should automatically run in
        self.workflows = ["default"]

        # Yara rule for sysprep.inf detection
        self.yara_rule = yara_x.compile("""
rule Windows_Unattended_Answer_File {
    meta:
        description = "Detects Windows unattended installation answer files"
        reference = "Windows unattended installation documentation"
        filetype = "INF"

    strings:
        // Required section headers
        $section_unattended = "[Unattended]" nocase
        $section_gui = "[GuiUnattended]" nocase
        $section_userdata = "[UserData]" nocase
        $section_setup = "[SetupMgr]" nocase

        // Common configuration keys
        $key1 = "OemSkipEula=" nocase
        $key2 = "InstallFilesPath=" nocase
        $key3 = "AdminPassword=" nocase
        $key4 = "EncryptedAdminPassword=" nocase
        $key5 = "ProductKey=" nocase
        $key6 = "ComputerName=" nocase

        // Network and domain related keys
        $net1 = "JoinDomain=" nocase
        $net2 = "DomainAdmin=" nocase
        $net3 = "DomainAdminPassword=" nocase

        // Common paths and shares
        $path1 = "C:\\\\sysprep" nocase
        $path2 = "i386" nocase
        $share = "DistShare=" nocase

    condition:
        2 of ($section_*) and
        4 of ($key*) and
        (2 of ($net*) or (2 of ($path*) and $share))
}
        """)

    def should_process(self, object_id: str) -> bool:
        """Determine if this module should run based on file type."""
        file_enriched = get_file_enriched(object_id)

        # Check basic conditions first
        if not (file_enriched.is_plaintext and file_enriched.file_name.lower() == "sysprep.inf"):
            return False

        # Run Yara scan
        file_bytes = self.storage.download_bytes(file_enriched.object_id)
        should_run = len(self.yara_rule.scan(file_bytes).matching_rules) > 0

        logger.debug(f"SysprepParser should_run: {should_run}, file: {file_enriched.file_name}")
        return should_run

    def _parse_sysprep_config(self, config_content: str) -> dict:
        """Parse the sysprep configuration file content."""
        config = {}
        current_section = None

        for line in config_content.splitlines():
            line = line.strip()

            # Skip empty lines and comments
            if not line or line.startswith(";") or line.startswith("#"):
                continue

            # Check for section headers
            if line.startswith("[") and line.endswith("]"):
                current_section = line[1:-1]
                config[current_section] = {}
                continue

            # Parse key-value pairs
            if "=" in line and current_section:
                key, value = line.split("=", 1)
                config[current_section][key.strip()] = value.strip()

        return config

    def _create_finding_summary(self, config: dict) -> str:
        """Creates a markdown summary for the sysprep credentials finding."""
        summary = "# Sysprep Credentials Detected\n\n"

        # Extract relevant sections
        gui_unattended = config.get("GuiUnattended", {})
        identification = config.get("Identification", {})

        # Add credentials if present
        if gui_unattended.get("AdminPassword"):
            summary += f"* **Local Admin Password**: `{gui_unattended['AdminPassword']}`\n"

        if identification:
            if identification.get("JoinDomain"):
                summary += f"* **Domain**: `{identification['JoinDomain']}`\n"
            if identification.get("DomainAdmin"):
                summary += f"* **Domain Admin**: `{identification['DomainAdmin']}`\n"
            if identification.get("DomainAdminPassword"):
                summary += f"* **Domain Admin Password**: `{identification['DomainAdminPassword']}`\n"

        return summary

    def _has_real_credentials(self, config: dict) -> bool:
        """Check if the config contains actual credentials (not placeholder or asterisks)."""

        def is_placeholder(value: str) -> bool:
            return value.strip("*") == "" or value.lower() in ["yourpassword", "password"]

        gui_unattended = config.get("GuiUnattended", {})
        identification = config.get("Identification", {})

        # Check each credential field
        credentials = [
            gui_unattended.get("AdminPassword"),
            identification.get("DomainAdmin"),
            identification.get("DomainAdminPassword"),
        ]

        # Return True if any credential is present and not a placeholder
        return any(cred for cred in credentials if cred and not is_placeholder(cred))

    def process(self, object_id: str) -> EnrichmentResult | None:
        """Process sysprep config file and extract credentials."""
        try:
            file_enriched = get_file_enriched(object_id)
            enrichment_result = EnrichmentResult(module_name=self.name, dependencies=self.dependencies)

            # Download and read the file
            with self.storage.download(file_enriched.object_id) as temp_file:
                content = Path(temp_file.name).read_text(encoding="utf-8")

                # Parse the configuration
                config = self._parse_sysprep_config(content)

                # Only create finding if real credentials are present
                if self._has_real_credentials(config):
                    # Create finding summary
                    summary_markdown = self._create_finding_summary(config)

                    # Create display data
                    display_data = FileObject(type="finding_summary", metadata={"summary": summary_markdown})

                    # Create finding
                    finding = Finding(
                        category=FindingCategory.CREDENTIAL,
                        finding_name="sysprep_credentials_detected",
                        origin_type=FindingOrigin.ENRICHMENT_MODULE,
                        origin_name=self.name,
                        object_id=file_enriched.object_id,
                        severity=8,
                        raw_data={"config": config},
                        data=[display_data],
                    )

                    # Add finding to enrichment result
                    enrichment_result.findings = [finding]

                enrichment_result.results = config

                # Create a displayable version of the results
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as tmp_display_file:
                    # Convert config to YAML with custom formatting
                    yaml_output = []
                    yaml_output.append("Sysprep Configuration Analysis")
                    yaml_output.append("===========================\n")

                    for section, values in config.items():
                        yaml_output.append(f"{section}:")
                        for key, value in values.items():
                            # Highlight sensitive fields
                            if key in ["AdminPassword", "DomainAdmin", "DomainAdminPassword"]:
                                yaml_output.append(f"   {key}: !!! {value} !!!")
                            else:
                                yaml_output.append(f"   {key}: {value}")
                        yaml_output.append("")  # Add empty line between sections

                    display = textwrap.indent("\n".join(yaml_output), "   ")
                    tmp_display_file.write(display)
                    tmp_display_file.flush()

                    object_id = self.storage.upload_file(tmp_display_file.name)

                    displayable_parsed = Transform(
                        type="displayable_parsed",
                        object_id=f"{object_id}",
                        metadata={
                            "file_name": f"{file_enriched.file_name}_analysis.txt",
                            "display_type_in_dashboard": "monaco",
                            "default_display": True,
                        },
                    )
                enrichment_result.transforms = [displayable_parsed]

            return enrichment_result

        except Exception as e:
            logger.exception(e, message="Error processing sysprep config file")
            return None


def create_enrichment_module() -> EnrichmentModule:
    return SysprepParser()
