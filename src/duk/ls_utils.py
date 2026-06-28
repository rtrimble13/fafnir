"""
List utilities for processing sector and industry list data.

This module provides functions for processing sector and industry
list data retrieved from APIs.
"""

import hashlib
import logging
from typing import Any, Dict, List, Optional

import pandas as pd

logger = logging.getLogger(__name__)


def _process_list_data(
    data: List[Dict[str, Any]],
    field_name: str,
    id_col: str,
    hash_col: str,
    name_col: str,
) -> pd.DataFrame:
    """
    Generic function to process list data with sorting and hash generation.

    Args:
        data: List of dictionaries containing the data
        field_name: Name of the field in input data (e.g., 'sector', 'industry')
        id_col: Name for the ID column in output (e.g., 'sector_id')
        hash_col: Name for the hash column in output (e.g., 'sector_hash')
        name_col: Name for the name column in output (e.g., 'sector_name')

    Returns:
        pandas DataFrame with three columns: id, hash, and name
    """
    if not data:
        logger.warning(f"Empty {field_name} data input, returning empty DataFrame")
        return pd.DataFrame(columns=[id_col, hash_col, name_col])

    logger.debug(f"Processing {len(data)} {field_name} records")

    # Create DataFrame from list of dictionaries
    df = pd.DataFrame(data)

    # Check if field exists
    if field_name not in df.columns:
        logger.warning(f"No '{field_name}' column found in data")
        return pd.DataFrame(columns=[id_col, hash_col, name_col])

    # Extract names and sort alphabetically
    names = df[field_name].tolist()
    names_sorted = sorted(names)

    # Create new DataFrame with processed data
    processed_data = []
    for idx, name in enumerate(names_sorted, start=1):
        # Generate SHA256 hash and take first 5 characters
        name_hash = hashlib.sha256(name.encode("utf-8")).hexdigest()[:5]

        processed_data.append(
            {
                id_col: idx,
                hash_col: name_hash,
                name_col: name,
            }
        )

    result_df = pd.DataFrame(processed_data)
    logger.info(f"Processed {len(result_df)} {field_name} records")

    return result_df


def process_sectors(sector_data: List[Dict[str, Any]]) -> pd.DataFrame:
    """
    Process sector list API data into a structured DataFrame.

    Takes the output of sector_list_api and converts it into a DataFrame
    with sector_id, sector_hash, and sector_name columns. Sectors are
    sorted alphabetically before assigning IDs.

    Args:
        sector_data: List of dictionaries containing sector information,
            as returned by sector_list_api. Each dictionary should
            contain a 'sector' field.

    Returns:
        pandas DataFrame with columns:
        - sector_id (int): Sequential ID starting from 1, assigned after
          alphabetical sorting
        - sector_hash (str): First 5 characters of SHA256 hash of sector name
        - sector_name (str): Name of the sector

    Example:
        >>> from duk.fmp_api import sector_list_api
        >>> sectors = sector_list_api("your_api_key")
        >>> df = process_sectors(sectors)
        >>> print(df.head())
    """
    return _process_list_data(
        data=sector_data,
        field_name="sector",
        id_col="sector_id",
        hash_col="sector_hash",
        name_col="sector_name",
    )


def process_industries(industry_data: List[Dict[str, Any]]) -> pd.DataFrame:
    """
    Process industry list API data into a structured DataFrame.

    Takes the output of industry_list_api and converts it into a DataFrame
    with industry_id, industry_hash, and industry_name columns. Industries
    are sorted alphabetically before assigning IDs.

    Args:
        industry_data: List of dictionaries containing industry information,
            as returned by industry_list_api. Each dictionary should
            contain an 'industry' field.

    Returns:
        pandas DataFrame with columns:
        - industry_id (int): Sequential ID starting from 1, assigned after
          alphabetical sorting
        - industry_hash (str): First 5 characters of SHA256 hash of industry name
        - industry_name (str): Name of the industry

    Example:
        >>> from duk.fmp_api import industry_list_api
        >>> industries = industry_list_api("your_api_key")
        >>> df = process_industries(industries)
        >>> print(df.head())
    """
    return _process_list_data(
        data=industry_data,
        field_name="industry",
        id_col="industry_id",
        hash_col="industry_hash",
        name_col="industry_name",
    )


def _get_sectors(
    api_key: str,
    sector_id: Optional[List[int]] = None,
    sector_hash: Optional[List[str]] = None,
) -> List[str]:
    """
    Get sector names filtered by numeric IDs or string hashes.

    Uses process_sectors and sector_list_api to query the sector list and
    filter it based on the input parameters. Either sector_id or sector_hash
    must be provided, but not both.

    Args:
        api_key: FMP API key for authentication
        sector_id: Optional list of numeric sector IDs to filter by
        sector_hash: Optional list of string hashes to filter by

    Returns:
        List of sector names matching the filter criteria

    Raises:
        ValueError: If both or neither filter parameters are provided

    Example:
        >>> sectors = _get_sectors("your_api_key", sector_id=[1, 2, 3])
        >>> sectors = _get_sectors("your_api_key", sector_hash=["abc12", "def34"])
    """
    from duk.fmp_api import sector_list_api

    if sector_id is None and sector_hash is None:
        raise ValueError("Either sector_id or sector_hash must be provided")
    if sector_id is not None and sector_hash is not None:
        raise ValueError("Cannot provide both sector_id and sector_hash")

    # Get all sectors from API
    sector_data = sector_list_api(api_key)
    df = process_sectors(sector_data)

    logger.debug(f"Retrieved {len(df)} sectors from API")

    # Filter based on provided parameter
    if sector_id is not None:
        filtered_df = df[df["sector_id"].isin(sector_id)]
        logger.debug(f"Filtered to {len(filtered_df)} sectors by ID")
    else:  # sector_hash is not None
        filtered_df = df[df["sector_hash"].isin(sector_hash)]
        logger.debug(f"Filtered to {len(filtered_df)} sectors by hash")

    result = filtered_df["sector_name"].tolist()
    logger.info(f"Returning {len(result)} sector names")
    return result


def _get_industries(
    api_key: str,
    industry_id: Optional[List[int]] = None,
    industry_hash: Optional[List[str]] = None,
) -> List[str]:
    """
    Get industry names filtered by numeric IDs or string hashes.

    Uses process_industries and industry_list_api to query the industry list
    and filter it based on the input parameters. Either industry_id or
    industry_hash must be provided, but not both.

    Args:
        api_key: FMP API key for authentication
        industry_id: Optional list of numeric industry IDs to filter by
        industry_hash: Optional list of string hashes to filter by

    Returns:
        List of industry names matching the filter criteria

    Raises:
        ValueError: If both or neither filter parameters are provided

    Example:
        >>> industries = _get_industries("your_api_key", industry_id=[1, 2, 3])
        >>> industries = _get_industries(
        ...     "your_api_key", industry_hash=["abc12", "def34"]
        ... )
    """
    from duk.fmp_api import industry_list_api

    if industry_id is None and industry_hash is None:
        raise ValueError("Either industry_id or industry_hash must be provided")
    if industry_id is not None and industry_hash is not None:
        raise ValueError("Cannot provide both industry_id and industry_hash")

    # Get all industries from API
    industry_data = industry_list_api(api_key)
    df = process_industries(industry_data)

    logger.debug(f"Retrieved {len(df)} industries from API")

    # Filter based on provided parameter
    if industry_id is not None:
        filtered_df = df[df["industry_id"].isin(industry_id)]
        logger.debug(f"Filtered to {len(filtered_df)} industries by ID")
    else:  # industry_hash is not None
        filtered_df = df[df["industry_hash"].isin(industry_hash)]
        logger.debug(f"Filtered to {len(filtered_df)} industries by hash")

    result = filtered_df["industry_name"].tolist()
    logger.info(f"Returning {len(result)} industry names")
    return result


def _screen_securities(
    api_key: str,
    marketCapMoreThan: Optional[float] = None,
    marketCapLowerThan: Optional[float] = None,
    sector: Optional[str] = None,
    industry: Optional[str] = None,
    betaMoreThan: Optional[float] = None,
    betaLowerThan: Optional[float] = None,
    priceMoreThan: Optional[float] = None,
    priceLowerThan: Optional[float] = None,
    dividendMoreThan: Optional[float] = None,
    dividendLowerThan: Optional[float] = None,
    volumeMoreThan: Optional[int] = None,
    volumeLowerThan: Optional[int] = None,
    exchange: Optional[str] = None,
    country: Optional[str] = None,
    isEtf: Optional[bool] = None,
    isFund: Optional[bool] = None,
    isActivelyTrading: Optional[bool] = None,
    limit: Optional[int] = None,
) -> pd.DataFrame:
    """
    Screen securities using FMP API and return results as a DataFrame.

    Takes the same parameters as screener_api and returns a pandas DataFrame
    of the results, sorted alphabetically by companyName.

    Args:
        api_key: FMP API key for authentication
        marketCapMoreThan: Optional minimum market capitalization
        marketCapLowerThan: Optional maximum market capitalization
        sector: Optional sector filter (e.g., "Technology")
        industry: Optional industry filter (e.g., "Consumer Electronics")
        betaMoreThan: Optional minimum beta value
        betaLowerThan: Optional maximum beta value
        priceMoreThan: Optional minimum stock price
        priceLowerThan: Optional maximum stock price
        dividendMoreThan: Optional minimum dividend
        dividendLowerThan: Optional maximum dividend
        volumeMoreThan: Optional minimum trading volume
        volumeLowerThan: Optional maximum trading volume
        exchange: Optional exchange filter (e.g., "NASDAQ")
        country: Optional country filter (e.g., "US")
        isEtf: Optional filter for ETFs (True/False)
        isFund: Optional filter for funds (True/False)
        isActivelyTrading: Optional filter for actively trading securities (True/False)
        limit: Optional maximum number of results to return

    Returns:
        pandas DataFrame with screening results, sorted alphabetically by
        companyName. Returns empty DataFrame if no results found.

    Example:
        >>> df = _screen_securities("your_api_key", sector="Technology", limit=100)
        >>> df = _screen_securities(
        ...     "your_api_key", industry="Software", priceMoreThan=10
        ... )
    """
    from duk.fmp_api import screener_api

    logger.debug("Screening securities with provided filters")

    # Call the screener API
    results = screener_api(
        api_key=api_key,
        marketCapMoreThan=marketCapMoreThan,
        marketCapLowerThan=marketCapLowerThan,
        sector=sector,
        industry=industry,
        betaMoreThan=betaMoreThan,
        betaLowerThan=betaLowerThan,
        priceMoreThan=priceMoreThan,
        priceLowerThan=priceLowerThan,
        dividendMoreThan=dividendMoreThan,
        dividendLowerThan=dividendLowerThan,
        volumeMoreThan=volumeMoreThan,
        volumeLowerThan=volumeLowerThan,
        exchange=exchange,
        country=country,
        isEtf=isEtf,
        isFund=isFund,
        isActivelyTrading=isActivelyTrading,
        limit=limit,
    )

    # Convert to DataFrame
    if not results:
        logger.warning("No screening results returned")
        return pd.DataFrame()

    df = pd.DataFrame(results)

    # Sort by companyName if it exists, otherwise try 'name'
    if "companyName" in df.columns:
        df = df.sort_values("companyName")
        logger.info(f"Returning {len(df)} securities sorted by companyName")
    elif "name" in df.columns:
        df = df.sort_values("name")
        logger.info(f"Returning {len(df)} securities sorted by name")
    else:
        logger.warning("No name column found, returning unsorted results")

    return df


def screen_securities(
    api_key: str,
    marketCapMoreThan: Optional[float] = None,
    marketCapLowerThan: Optional[float] = None,
    sector: Optional[List[str]] = None,
    industry: Optional[List[str]] = None,
    betaMoreThan: Optional[float] = None,
    betaLowerThan: Optional[float] = None,
    priceMoreThan: Optional[float] = None,
    priceLowerThan: Optional[float] = None,
    dividendMoreThan: Optional[float] = None,
    dividendLowerThan: Optional[float] = None,
    volumeMoreThan: Optional[int] = None,
    volumeLowerThan: Optional[int] = None,
    exchange: Optional[str] = None,
    country: Optional[str] = None,
    isEtf: Optional[bool] = None,
    isFund: Optional[bool] = None,
    isActivelyTrading: Optional[bool] = None,
    limit: Optional[int] = None,
) -> pd.DataFrame:
    """
    Screen securities with support for multiple sectors and industries.

    Takes the same parameters as _screen_securities, except that 'sector' and
    'industry' parameters are optional lists of strings. If either parameter
    has more than 1 entry, calls _screen_securities for each entry and
    combines results.

    Args:
        api_key: FMP API key for authentication
        marketCapMoreThan: Optional minimum market capitalization
        marketCapLowerThan: Optional maximum market capitalization
        sector: Optional list of sector filters (e.g., ["Technology", "Healthcare"])
        industry: Optional list of industry filters (e.g., ["Software", "Biotech"])
        betaMoreThan: Optional minimum beta value
        betaLowerThan: Optional maximum beta value
        priceMoreThan: Optional minimum stock price
        priceLowerThan: Optional maximum stock price
        dividendMoreThan: Optional minimum dividend
        dividendLowerThan: Optional maximum dividend
        volumeMoreThan: Optional minimum trading volume
        volumeLowerThan: Optional maximum trading volume
        exchange: Optional exchange filter (e.g., "NASDAQ")
        country: Optional country filter (e.g., "US")
        isEtf: Optional filter for ETFs (True/False)
        isFund: Optional filter for funds (True/False)
        isActivelyTrading: Optional filter for actively trading securities (True/False)
        limit: Optional maximum number of results to return per screen

    Returns:
        pandas DataFrame with combined screening results, sorted alphabetically
        by companyName. Duplicate entries are removed.

    Example:
        >>> # Screen single sector
        >>> df = screen_securities("your_api_key", sector=["Technology"])
        >>> # Screen multiple sectors
        >>> df = screen_securities("your_api_key",
        ...                        sector=["Technology", "Healthcare"],
        ...                        priceMoreThan=10)
        >>> # Screen multiple industries
        >>> df = screen_securities("your_api_key",
        ...                        industry=["Software", "Biotech", "Banking"])
    """
    logger.debug("Starting screen_securities with multiple sector/industry support")

    all_results = []

    # Screen each sector separately
    if sector:
        for sector_value in sector:
            logger.debug(f"Screening with sector={sector_value}")

            df = _screen_securities(
                api_key=api_key,
                marketCapMoreThan=marketCapMoreThan,
                marketCapLowerThan=marketCapLowerThan,
                sector=sector_value,
                industry=None,
                betaMoreThan=betaMoreThan,
                betaLowerThan=betaLowerThan,
                priceMoreThan=priceMoreThan,
                priceLowerThan=priceLowerThan,
                dividendMoreThan=dividendMoreThan,
                dividendLowerThan=dividendLowerThan,
                volumeMoreThan=volumeMoreThan,
                volumeLowerThan=volumeLowerThan,
                exchange=exchange,
                country=country,
                isEtf=isEtf,
                isFund=isFund,
                isActivelyTrading=isActivelyTrading,
                limit=limit,
            )

            if not df.empty:
                all_results.append(df)

    # Screen each industry separately
    if industry:
        for industry_value in industry:
            logger.debug(f"Screening with industry={industry_value}")

            df = _screen_securities(
                api_key=api_key,
                marketCapMoreThan=marketCapMoreThan,
                marketCapLowerThan=marketCapLowerThan,
                sector=None,
                industry=industry_value,
                betaMoreThan=betaMoreThan,
                betaLowerThan=betaLowerThan,
                priceMoreThan=priceMoreThan,
                priceLowerThan=priceLowerThan,
                dividendMoreThan=dividendMoreThan,
                dividendLowerThan=dividendLowerThan,
                volumeMoreThan=volumeMoreThan,
                volumeLowerThan=volumeLowerThan,
                exchange=exchange,
                country=country,
                isEtf=isEtf,
                isFund=isFund,
                isActivelyTrading=isActivelyTrading,
                limit=limit,
            )

            if not df.empty:
                all_results.append(df)

    # If neither sector nor industry is provided, screen without filters
    if not sector and not industry:
        logger.debug("Screening without sector or industry filters")

        df = _screen_securities(
            api_key=api_key,
            marketCapMoreThan=marketCapMoreThan,
            marketCapLowerThan=marketCapLowerThan,
            sector=None,
            industry=None,
            betaMoreThan=betaMoreThan,
            betaLowerThan=betaLowerThan,
            priceMoreThan=priceMoreThan,
            priceLowerThan=priceLowerThan,
            dividendMoreThan=dividendMoreThan,
            dividendLowerThan=dividendLowerThan,
            volumeMoreThan=volumeMoreThan,
            volumeLowerThan=volumeLowerThan,
            exchange=exchange,
            country=country,
            isEtf=isEtf,
            isFund=isFund,
            isActivelyTrading=isActivelyTrading,
            limit=limit,
        )

        if not df.empty:
            all_results.append(df)

    # Combine all results
    if not all_results:
        logger.warning("No results from any screen")
        return pd.DataFrame()

    combined_df = pd.concat(all_results, ignore_index=True)

    # Remove duplicates based on symbol if it exists
    if "symbol" in combined_df.columns:
        combined_df = combined_df.drop_duplicates(subset=["symbol"])
        logger.debug(f"Removed duplicates, {len(combined_df)} unique securities remain")
        # Set symbol as the index
        combined_df = combined_df.set_index("symbol")
        logger.debug("Set symbol as DataFrame index")

    # Sort by companyName if it exists, otherwise try 'name'
    if "companyName" in combined_df.columns:
        combined_df = combined_df.sort_values("companyName")
        logger.info(
            f"Returning {len(combined_df)} unique securities sorted by companyName"
        )
    elif "name" in combined_df.columns:
        combined_df = combined_df.sort_values("name")
        logger.info(f"Returning {len(combined_df)} unique securities sorted by name")

    return combined_df
