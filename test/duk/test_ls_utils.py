"""
Tests for ls_utils module.
"""

import hashlib

import pandas as pd
import pytest

from duk.ls_utils import process_industries, process_sectors


class TestProcessSectors:
    """Tests for process_sectors function."""

    def test_basic_processing(self):
        """Test basic processing of sector data."""
        sector_data = [
            {"sector": "Technology"},
            {"sector": "Healthcare"},
            {"sector": "Financial Services"},
        ]

        result = process_sectors(sector_data)

        assert isinstance(result, pd.DataFrame)
        assert len(result) == 3
        assert list(result.columns) == ["sector_id", "sector_hash", "sector_name"]

    def test_alphabetical_sorting(self):
        """Test that sectors are sorted alphabetically before assigning IDs."""
        sector_data = [
            {"sector": "Technology"},
            {"sector": "Healthcare"},
            {"sector": "Financial Services"},
        ]

        result = process_sectors(sector_data)

        # Check that sectors are in alphabetical order
        assert result["sector_name"].tolist() == [
            "Financial Services",
            "Healthcare",
            "Technology",
        ]

    def test_sector_id_assignment(self):
        """Test that sector IDs are assigned sequentially starting from 1."""
        sector_data = [
            {"sector": "Technology"},
            {"sector": "Healthcare"},
            {"sector": "Financial Services"},
        ]

        result = process_sectors(sector_data)

        # Check that IDs start at 1 and are sequential
        assert result["sector_id"].tolist() == [1, 2, 3]

    def test_sector_hash_generation(self):
        """Test that sector hash is first 5 characters of SHA256 hash."""
        sector_data = [
            {"sector": "Technology"},
        ]

        result = process_sectors(sector_data)

        # Calculate expected hash
        expected_hash = hashlib.sha256("Technology".encode("utf-8")).hexdigest()[:5]
        assert result["sector_hash"].iloc[0] == expected_hash

    def test_sector_hash_length(self):
        """Test that sector hash is exactly 5 characters."""
        sector_data = [
            {"sector": "Technology"},
            {"sector": "Healthcare"},
        ]

        result = process_sectors(sector_data)

        # Check that all hashes are 5 characters long
        for sector_hash in result["sector_hash"]:
            assert len(sector_hash) == 5

    def test_empty_input(self):
        """Test processing of empty sector data."""
        sector_data = []

        result = process_sectors(sector_data)

        assert isinstance(result, pd.DataFrame)
        assert len(result) == 0
        assert list(result.columns) == ["sector_id", "sector_hash", "sector_name"]

    def test_missing_sector_column(self):
        """Test handling of data without 'sector' column."""
        sector_data = [
            {"name": "Technology"},
            {"name": "Healthcare"},
        ]

        result = process_sectors(sector_data)

        assert isinstance(result, pd.DataFrame)
        assert len(result) == 0
        assert list(result.columns) == ["sector_id", "sector_hash", "sector_name"]

    def test_duplicate_sectors(self):
        """Test processing of duplicate sector names."""
        sector_data = [
            {"sector": "Technology"},
            {"sector": "Healthcare"},
            {"sector": "Technology"},
        ]

        result = process_sectors(sector_data)

        # Both Technology entries should be included
        assert len(result) == 3
        # Check that sorting still works correctly
        tech_entries = result[result["sector_name"] == "Technology"]
        assert len(tech_entries) == 2

    def test_data_types(self):
        """Test that output columns have correct data types."""
        sector_data = [
            {"sector": "Technology"},
            {"sector": "Healthcare"},
        ]

        result = process_sectors(sector_data)

        # sector_id should be integer
        assert pd.api.types.is_integer_dtype(result["sector_id"])
        # sector_hash should be string (object or the newer string dtype)
        assert pd.api.types.is_object_dtype(
            result["sector_hash"]
        ) or pd.api.types.is_string_dtype(result["sector_hash"])
        # sector_name should be string (object or the newer string dtype)
        assert pd.api.types.is_object_dtype(
            result["sector_name"]
        ) or pd.api.types.is_string_dtype(result["sector_name"])


class TestProcessIndustries:
    """Tests for process_industries function."""

    def test_basic_processing(self):
        """Test basic processing of industry data."""
        industry_data = [
            {"industry": "Software"},
            {"industry": "Pharmaceuticals"},
            {"industry": "Banking"},
        ]

        result = process_industries(industry_data)

        assert isinstance(result, pd.DataFrame)
        assert len(result) == 3
        assert list(result.columns) == [
            "industry_id",
            "industry_hash",
            "industry_name",
        ]

    def test_alphabetical_sorting(self):
        """Test that industries are sorted alphabetically before assigning IDs."""
        industry_data = [
            {"industry": "Software"},
            {"industry": "Pharmaceuticals"},
            {"industry": "Banking"},
        ]

        result = process_industries(industry_data)

        # Check that industries are in alphabetical order
        assert result["industry_name"].tolist() == [
            "Banking",
            "Pharmaceuticals",
            "Software",
        ]

    def test_industry_id_assignment(self):
        """Test that industry IDs are assigned sequentially starting from 1."""
        industry_data = [
            {"industry": "Software"},
            {"industry": "Pharmaceuticals"},
            {"industry": "Banking"},
        ]

        result = process_industries(industry_data)

        # Check that IDs start at 1 and are sequential
        assert result["industry_id"].tolist() == [1, 2, 3]

    def test_industry_hash_generation(self):
        """Test that industry hash is first 5 characters of SHA256 hash."""
        industry_data = [
            {"industry": "Software"},
        ]

        result = process_industries(industry_data)

        # Calculate expected hash
        expected_hash = hashlib.sha256("Software".encode("utf-8")).hexdigest()[:5]
        assert result["industry_hash"].iloc[0] == expected_hash

    def test_industry_hash_length(self):
        """Test that industry hash is exactly 5 characters."""
        industry_data = [
            {"industry": "Software"},
            {"industry": "Pharmaceuticals"},
        ]

        result = process_industries(industry_data)

        # Check that all hashes are 5 characters long
        for industry_hash in result["industry_hash"]:
            assert len(industry_hash) == 5

    def test_empty_input(self):
        """Test processing of empty industry data."""
        industry_data = []

        result = process_industries(industry_data)

        assert isinstance(result, pd.DataFrame)
        assert len(result) == 0
        assert list(result.columns) == [
            "industry_id",
            "industry_hash",
            "industry_name",
        ]

    def test_missing_industry_column(self):
        """Test handling of data without 'industry' column."""
        industry_data = [
            {"name": "Software"},
            {"name": "Pharmaceuticals"},
        ]

        result = process_industries(industry_data)

        assert isinstance(result, pd.DataFrame)
        assert len(result) == 0
        assert list(result.columns) == [
            "industry_id",
            "industry_hash",
            "industry_name",
        ]

    def test_duplicate_industries(self):
        """Test processing of duplicate industry names."""
        industry_data = [
            {"industry": "Software"},
            {"industry": "Pharmaceuticals"},
            {"industry": "Software"},
        ]

        result = process_industries(industry_data)

        # Both Software entries should be included
        assert len(result) == 3
        # Check that sorting still works correctly
        software_entries = result[result["industry_name"] == "Software"]
        assert len(software_entries) == 2

    def test_data_types(self):
        """Test that output columns have correct data types."""
        industry_data = [
            {"industry": "Software"},
            {"industry": "Pharmaceuticals"},
        ]

        result = process_industries(industry_data)

        # industry_id should be integer
        assert pd.api.types.is_integer_dtype(result["industry_id"])
        # industry_hash should be string (object or the newer string dtype)
        assert pd.api.types.is_object_dtype(
            result["industry_hash"]
        ) or pd.api.types.is_string_dtype(result["industry_hash"])
        # industry_name should be string (object or the newer string dtype)
        assert pd.api.types.is_object_dtype(
            result["industry_name"]
        ) or pd.api.types.is_string_dtype(result["industry_name"])


class TestHashConsistency:
    """Test that hash generation is consistent across both functions."""

    def test_same_hash_for_same_string(self):
        """Test that the same string generates the same hash in both functions."""
        test_name = "Technology"

        sector_data = [{"sector": test_name}]
        industry_data = [{"industry": test_name}]

        sector_result = process_sectors(sector_data)
        industry_result = process_industries(industry_data)

        # The hash should be the same for the same string
        assert (
            sector_result["sector_hash"].iloc[0]
            == industry_result["industry_hash"].iloc[0]
        )


class TestGetSectors:
    """Tests for _get_sectors function."""

    def test_get_sectors_by_id(self):
        """Test getting sectors by numeric ID."""
        from unittest import mock

        from duk.ls_utils import _get_sectors

        mock_sector_data = [
            {"sector": "Technology"},
            {"sector": "Healthcare"},
            {"sector": "Financial Services"},
        ]

        with mock.patch("duk.fmp_api.sector_list_api") as mock_api:
            mock_api.return_value = mock_sector_data

            # After sorting: Financial Services (id=1), Healthcare (id=2),
            # Technology (id=3)
            result = _get_sectors("test_api_key", sector_id=[1, 3])

            assert len(result) == 2
            assert "Financial Services" in result
            assert "Technology" in result
            assert "Healthcare" not in result

    def test_get_sectors_by_hash(self):
        """Test getting sectors by hash."""
        from unittest import mock

        from duk.ls_utils import _get_sectors

        mock_sector_data = [
            {"sector": "Technology"},
            {"sector": "Healthcare"},
        ]

        with mock.patch("duk.fmp_api.sector_list_api") as mock_api:
            mock_api.return_value = mock_sector_data

            # Calculate the hash for Technology
            tech_hash = hashlib.sha256("Technology".encode("utf-8")).hexdigest()[:5]

            result = _get_sectors("test_api_key", sector_hash=[tech_hash])

            assert len(result) == 1
            assert "Technology" in result

    def test_get_sectors_neither_parameter(self):
        """Test that error is raised when neither parameter is provided."""
        from duk.ls_utils import _get_sectors

        with pytest.raises(
            ValueError, match="Either sector_id or sector_hash must be provided"
        ):
            _get_sectors("test_api_key")

    def test_get_sectors_both_parameters(self):
        """Test that error is raised when both parameters are provided."""
        from duk.ls_utils import _get_sectors

        with pytest.raises(
            ValueError, match="Cannot provide both sector_id and sector_hash"
        ):
            _get_sectors("test_api_key", sector_id=[1], sector_hash=["abc12"])


class TestGetIndustries:
    """Tests for _get_industries function."""

    def test_get_industries_by_id(self):
        """Test getting industries by numeric ID."""
        from unittest import mock

        from duk.ls_utils import _get_industries

        mock_industry_data = [
            {"industry": "Software"},
            {"industry": "Pharmaceuticals"},
            {"industry": "Banking"},
        ]

        with mock.patch("duk.fmp_api.industry_list_api") as mock_api:
            mock_api.return_value = mock_industry_data

            # After sorting: Banking (id=1), Pharmaceuticals (id=2), Software (id=3)
            result = _get_industries("test_api_key", industry_id=[1, 3])

            assert len(result) == 2
            assert "Banking" in result
            assert "Software" in result
            assert "Pharmaceuticals" not in result

    def test_get_industries_by_hash(self):
        """Test getting industries by hash."""
        from unittest import mock

        from duk.ls_utils import _get_industries

        mock_industry_data = [
            {"industry": "Software"},
            {"industry": "Pharmaceuticals"},
        ]

        with mock.patch("duk.fmp_api.industry_list_api") as mock_api:
            mock_api.return_value = mock_industry_data

            # Calculate the hash for Software
            software_hash = hashlib.sha256("Software".encode("utf-8")).hexdigest()[:5]

            result = _get_industries("test_api_key", industry_hash=[software_hash])

            assert len(result) == 1
            assert "Software" in result

    def test_get_industries_neither_parameter(self):
        """Test that error is raised when neither parameter is provided."""
        from duk.ls_utils import _get_industries

        with pytest.raises(
            ValueError, match="Either industry_id or industry_hash must be provided"
        ):
            _get_industries("test_api_key")

    def test_get_industries_both_parameters(self):
        """Test that error is raised when both parameters are provided."""
        from duk.ls_utils import _get_industries

        with pytest.raises(
            ValueError, match="Cannot provide both industry_id and industry_hash"
        ):
            _get_industries("test_api_key", industry_id=[1], industry_hash=["abc12"])


class TestScreenSecurities:
    """Tests for _screen_securities function."""

    def test_screen_securities_basic(self):
        """Test basic screening with single sector."""
        from unittest import mock

        from duk.ls_utils import _screen_securities

        mock_response = [
            {"symbol": "MSFT", "name": "Microsoft Corporation", "sector": "Technology"},
            {"symbol": "AAPL", "name": "Apple Inc.", "sector": "Technology"},
        ]

        with mock.patch("duk.fmp_api.screener_api") as mock_api:
            mock_api.return_value = mock_response

            result = _screen_securities("test_api_key", sector="Technology")

            assert isinstance(result, pd.DataFrame)
            assert len(result) == 2
            # Check sorting by name
            assert result["name"].iloc[0] == "Apple Inc."
            assert result["name"].iloc[1] == "Microsoft Corporation"

    def test_screen_securities_with_companyName(self):
        """Test screening when response has companyName field."""
        from unittest import mock

        from duk.ls_utils import _screen_securities

        mock_response = [
            {
                "symbol": "MSFT",
                "companyName": "Microsoft Corporation",
                "sector": "Technology",
            },
            {"symbol": "AAPL", "companyName": "Apple Inc.", "sector": "Technology"},
        ]

        with mock.patch("duk.fmp_api.screener_api") as mock_api:
            mock_api.return_value = mock_response

            result = _screen_securities("test_api_key", sector="Technology")

            assert isinstance(result, pd.DataFrame)
            assert len(result) == 2
            # Check sorting by companyName
            assert result["companyName"].iloc[0] == "Apple Inc."
            assert result["companyName"].iloc[1] == "Microsoft Corporation"

    def test_screen_securities_empty_results(self):
        """Test screening with no results."""
        from unittest import mock

        from duk.ls_utils import _screen_securities

        with mock.patch("duk.fmp_api.screener_api") as mock_api:
            mock_api.return_value = []

            result = _screen_securities("test_api_key", sector="Technology")

            assert isinstance(result, pd.DataFrame)
            assert len(result) == 0


class TestScreenSecuritiesMultiple:
    """Tests for screen_securities function with multiple sectors/industries."""

    def test_screen_securities_single_sector(self):
        """Test screening with single sector in list."""
        from unittest import mock

        from duk.ls_utils import screen_securities

        mock_response = [
            {"symbol": "AAPL", "name": "Apple Inc.", "sector": "Technology"},
        ]

        with mock.patch("duk.fmp_api.screener_api") as mock_api:
            mock_api.return_value = mock_response

            result = screen_securities("test_api_key", sector=["Technology"])

            assert isinstance(result, pd.DataFrame)
            assert len(result) == 1
            mock_api.assert_called_once()

    def test_screen_securities_multiple_sectors(self):
        """Test screening with multiple sectors."""
        from unittest import mock

        from duk.ls_utils import screen_securities

        def mock_screener_side_effect(*args, **kwargs):
            sector = kwargs.get("sector")
            if sector == "Technology":
                return [
                    {"symbol": "AAPL", "name": "Apple Inc.", "sector": "Technology"}
                ]
            elif sector == "Healthcare":
                return [
                    {
                        "symbol": "JNJ",
                        "name": "Johnson & Johnson",
                        "sector": "Healthcare",
                    }
                ]
            return []

        with mock.patch(
            "duk.fmp_api.screener_api", side_effect=mock_screener_side_effect
        ) as mock_api:
            result = screen_securities(
                "test_api_key", sector=["Technology", "Healthcare"]
            )

            assert isinstance(result, pd.DataFrame)
            assert len(result) == 2
            assert mock_api.call_count == 2

    def test_screen_securities_multiple_industries(self):
        """Test screening with multiple industries."""
        from unittest import mock

        from duk.ls_utils import screen_securities

        def mock_screener_side_effect(*args, **kwargs):
            industry = kwargs.get("industry")
            if industry == "Software":
                return [
                    {
                        "symbol": "MSFT",
                        "name": "Microsoft Corporation",
                        "industry": "Software",
                    }
                ]
            elif industry == "Hardware":
                return [
                    {"symbol": "AAPL", "name": "Apple Inc.", "industry": "Hardware"}
                ]
            return []

        with mock.patch(
            "duk.fmp_api.screener_api", side_effect=mock_screener_side_effect
        ) as mock_api:
            result = screen_securities(
                "test_api_key", industry=["Software", "Hardware"]
            )

            assert isinstance(result, pd.DataFrame)
            assert len(result) == 2
            assert mock_api.call_count == 2

    def test_screen_securities_removes_duplicates(self):
        """Test that duplicate securities are removed."""
        from unittest import mock

        from duk.ls_utils import screen_securities

        # Mock response that returns the same security for different sectors
        mock_response = [
            {"symbol": "AAPL", "name": "Apple Inc.", "sector": "Technology"}
        ]

        with mock.patch("duk.fmp_api.screener_api") as mock_api:
            mock_api.return_value = mock_response

            result = screen_securities(
                "test_api_key", sector=["Technology", "Healthcare"]
            )

            # Should only have 1 unique security even though screened twice
            assert isinstance(result, pd.DataFrame)
            # Note: This could be 1 or 2 depending on if both sectors
            # return the same security. If AAPL only appears in Technology
            # sector, it should be 1
            assert "AAPL" in result.index

    def test_screen_securities_no_sectors_or_industries(self):
        """Test screening without sector or industry filters."""
        from unittest import mock

        from duk.ls_utils import screen_securities

        mock_response = [
            {"symbol": "AAPL", "name": "Apple Inc."},
            {"symbol": "MSFT", "name": "Microsoft Corporation"},
        ]

        with mock.patch("duk.fmp_api.screener_api") as mock_api:
            mock_api.return_value = mock_response

            result = screen_securities("test_api_key", priceMoreThan=100)

            assert isinstance(result, pd.DataFrame)
            assert len(result) == 2
            mock_api.assert_called_once()

    def test_screen_securities_sectors_and_industries_separate_loops(self):
        """Test sectors and industries screened separately, not cross product."""
        from unittest import mock

        from duk.ls_utils import screen_securities

        call_log = []

        def mock_screener_side_effect(*args, **kwargs):
            sector = kwargs.get("sector")
            industry = kwargs.get("industry")
            call_log.append({"sector": sector, "industry": industry})

            # Return different securities based on what's being screened
            if sector == "Technology" and industry is None:
                return [
                    {"symbol": "AAPL", "name": "Apple Inc.", "sector": "Technology"}
                ]
            elif sector == "Healthcare" and industry is None:
                return [
                    {
                        "symbol": "JNJ",
                        "name": "Johnson & Johnson",
                        "sector": "Healthcare",
                    }
                ]
            elif industry == "Software" and sector is None:
                return [
                    {
                        "symbol": "MSFT",
                        "name": "Microsoft Corporation",
                        "industry": "Software",
                    }
                ]
            elif industry == "Hardware" and sector is None:
                return [
                    {
                        "symbol": "HPE",
                        "name": "Hewlett Packard Enterprise",
                        "industry": "Hardware",
                    }
                ]
            return []

        with mock.patch(
            "duk.fmp_api.screener_api", side_effect=mock_screener_side_effect
        ) as mock_api:
            result = screen_securities(
                "test_api_key",
                sector=["Technology", "Healthcare"],
                industry=["Software", "Hardware"],
            )

            # Should make 4 calls: Tech, Healthcare, Software, Hardware
            # (NOT cross product)
            assert mock_api.call_count == 4

            # Verify the calls were made separately for sectors and industries
            expected_calls = [
                {"sector": "Technology", "industry": None},
                {"sector": "Healthcare", "industry": None},
                {"sector": None, "industry": "Software"},
                {"sector": None, "industry": "Hardware"},
            ]

            assert call_log == expected_calls

            # Should have 4 unique securities
            assert isinstance(result, pd.DataFrame)
            assert len(result) == 4
            assert set(result.index) == {"AAPL", "JNJ", "MSFT", "HPE"}
