"""ADO organization case survives into every URL the kit sends (a path-scoped egress policy may be case-sensitive)."""
import unittest
from unittest.mock import patch

import review_ado
from review_common import AdoClient, canonical_ado_metadata_url, parse_ado_url

ORG_URL = "https://dev.azure.com/O365Exchange"


class OrganizationCaseTests(unittest.TestCase):
    def test_parsed_remote_keeps_the_organization_case(self):
        parsed = parse_ado_url("https://dev.azure.com/O365Exchange/Weve_DA/_git/Zoran")
        self.assertEqual(parsed["org_url"], ORG_URL)

    def test_api_clients_keep_the_organization_case(self):
        self.assertEqual(AdoClient(ORG_URL).org_url, ORG_URL)
        self.assertEqual(review_ado.AzureCliTransport(ORG_URL).org_url, ORG_URL)

    def test_metadata_url_from_the_same_org_in_another_case_is_accepted(self):
        url = canonical_ado_metadata_url("https://o365exchange@dev.azure.com/O365Exchange/Weve_DA/_git/Zoran",
                                         "https://dev.azure.com/o365exchange")
        self.assertEqual(url, "https://dev.azure.com/O365Exchange/Weve_DA/_git/Zoran")

    def test_one_organization_in_two_cases_still_resolves_to_one(self):
        modules = [{"provider": "ado", "org_url": ORG_URL}, {"provider": "ado", "org_url": "https://dev.azure.com/o365exchange"}]
        with patch.object(review_ado, "discover_root", return_value=None), \
                patch.object(review_ado, "read_submodules", return_value=modules):
            self.assertEqual(review_ado._resolve_org_url(None).casefold(), ORG_URL.casefold())


if __name__ == "__main__":
    unittest.main()
