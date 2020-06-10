import time
from datetime import date, datetime, timedelta
from typing import Dict, List

import alpaca_trade_api as tradeapi
import requests
from alpaca_trade_api.common import get_polygon_credentials
from alpaca_trade_api.polygon.entity import Ticker
from asyncpg.pool import Pool
from google.cloud import error_reporting
from pandas import DataFrame as df
from pandas import Timestamp
from pytz import timezone

from common import config, trading_data
from common.decorators import timeit
from common.tlog import tlog
from models.ticker_snapshot import TickerSnapshot

try:
    error_logger = error_reporting.Client()
except Exception:
    error_logger = None

volume_today: Dict[str, int] = {}
minute_history: Dict[str, df] = {}


def get_historical_data_from_finnhub(symbols: List[str]) -> Dict[str, df]:
    nyc = timezone(NY := "America/New_York")
    _from = datetime.today().astimezone(nyc) - timedelta(days=30)
    _from = _from.replace(hour=9, minute=29)
    _to = datetime.now(nyc)

    minute_history: Dict[str, df] = {}
    with requests.Session() as s:
        try:
            for symbol in symbols:
                retry = True
                while retry:
                    retry = False

                    url = f"{config.finnhub_base_url}/stock/candle?symbol={symbol}&resolution=1&from={_from.strftime('%s')}&to={_to.strftime('%s')}&token={config.finnhub_api_key}"
                    r = s.get(url)

                    if r.status_code == 200:
                        response = r.json()
                        if response["s"] != "no_data":
                            _data = {
                                "close": response["c"],
                                "open": response["o"],
                                "high": response["h"],
                                "low": response["l"],
                                "volume": response["v"],
                            }

                            _df = df(
                                _data,
                                index=[
                                    Timestamp(item, tz=NY, unit="s")
                                    for item in response["t"]
                                ],
                            )
                            _df["vwap"] = 0.0
                            _df["average"] = 0.0
                            minute_history[symbol] = _df

                            print(minute_history[symbol])
                    elif r.status_code == 429:
                        tlog(
                            f"{symbols.index(symbol)}/{len(symbols)} API limit: ({r.text})"
                        )
                        time.sleep(30)
                        retry = True
                    else:
                        print(r.status_code, r.text)

        except KeyboardInterrupt:
            tlog("KeyboardInterrupt")
            pass

    return minute_history


def get_historical_data_from_polygon(
    api: tradeapi, symbols: List[str], max_tickers: int
) -> Dict[str, df]:
    """get ticker history"""

    tlog(f"Loading max {max_tickers} tickers w/ highest volume from Polygon")
    minute_history: Dict[str, df] = {}
    c = 0
    exclude_symbols = []
    try:
        for symbol in symbols:
            if symbol not in minute_history:
                retry_counter = 5
                while retry_counter > 0:
                    try:
                        if c < max_tickers:
                            _df = api.polygon.historic_agg_v2(
                                symbol,
                                1,
                                "minute",
                                _from=str(date.today() - timedelta(days=10)),
                                to=str(date.today() + timedelta(days=1)),
                            ).df
                            _df["vwap"] = 0.0
                            _df["average"] = 0.0

                            minute_history[symbol] = _df
                            tlog(
                                f"loaded {len(minute_history[symbol].index)} agg data points for {symbol} {c}/{max_tickers}"
                            )
                            c += 1
                            break

                        exclude_symbols.append(symbol)
                        break
                    except (
                        requests.exceptions.HTTPError,
                        requests.exceptions.ConnectionError,
                    ):
                        retry_counter -= 1
                        if retry_counter == 0:
                            if error_logger:
                                error_logger.report_exception()
                            exclude_symbols.append(symbol)
    except KeyboardInterrupt:
        tlog("KeyboardInterrupt")

    for x in exclude_symbols:
        symbols.remove(x)

    tlog(f"Total number of symbols for trading {len(symbols)}")
    return minute_history


def get_finnhub_tickers(data_api: tradeapi) -> List[str]:
    tlog("get_finnhub_tickers(): Getting current ticker data...")

    assets = data_api.list_assets()
    tradable_symbols = [asset.symbol for asset in assets if asset.tradable]
    tlog(f"loaded {len(tradable_symbols)} assets from Alpaca")
    nyc = timezone("America/New_York")
    _from = datetime.today().astimezone(nyc) - timedelta(days=1)
    _to = datetime.now(nyc)
    symbols = []
    try:
        with requests.Session() as s:
            for symbol in tradable_symbols:
                try:
                    retry = True
                    while retry:
                        retry = False
                        url = f"{config.finnhub_base_url}/stock/candle?symbol={symbol}&resolution=D&from={_from.strftime('%s')}&to={_to.strftime('%s')}&token={config.finnhub_api_key}"

                        try:
                            r = s.get(url)
                        except ConnectionError:
                            retry = True
                            pass

                        if r.status_code == 200:
                            response = r.json()
                            if response["s"] != "no_data":
                                prev_prec = (
                                    100.0
                                    * (response["c"][1] - response["c"][0])
                                    / response["c"][0]
                                )
                                if (
                                    config.max_share_price
                                    > response["c"][1]
                                    > config.min_share_price
                                    and response["v"][0] * response["c"][0]
                                    > config.min_last_dv
                                    and prev_prec > config.today_change_percent
                                ):
                                    symbols.append(symbol)
                                    tlog(
                                        f"collected {len(symbols)}/{config.finnhub_websocket_limit}"
                                    )
                                    if (
                                        len(symbols)
                                        == config.finnhub_websocket_limit
                                    ):
                                        break

                        elif r.status_code == 429:
                            tlog(
                                f"{tradable_symbols.index(symbol)}/{len(tradable_symbols)} API limit: ({r.text})"
                            )
                            time.sleep(30)
                            retry = True
                        else:
                            print(r.status_code, r.text)
                except IndexError as e:
                    pass

    except KeyboardInterrupt:
        tlog("KeyboardInterrupt")
        pass

    tlog(f"loaded {len(symbols)} from Finnhub")
    return symbols


def get_polygon_tickers(data_api: tradeapi) -> List[Ticker]:
    """get all tickers"""

    tlog("get_polygon_tickers(): Getting current ticker data...")
    try:
        max_retries = 50
        while max_retries > 0:
            tickers = data_api.polygon.all_tickers()
            tlog(f"loaded {len(tickers)} tickers from Polygon")

            if not len(tickers):
                break
            assets = data_api.list_assets()
            tlog(f"loaded {len(assets)} assets from Alpaca")
            tradable_symbols = [
                asset.symbol for asset in assets if asset.tradable
            ]
            tlog(
                f"Total number of tradable symobls is {len(tradable_symbols)}"
            )
            unsorted = [
                ticker
                for ticker in tickers
                if (
                    ticker.ticker in tradable_symbols
                    and config.max_share_price
                    >= ticker.lastTrade["p"]
                    >= config.min_share_price
                    and ticker.prevDay["v"] * ticker.lastTrade["p"]
                    > config.min_last_dv
                    and ticker.todaysChangePerc >= config.today_change_percent
                )
            ]
            if len(unsorted) > 0:
                rc = sorted(
                    unsorted,
                    key=lambda ticker: float(ticker.day["v"]),
                    reverse=True,
                )
                tlog(f"loaded {len(rc)} tickers")
                return rc

            tlog("got no data :-( waiting then re-trying")
            time.sleep(30)
            max_retries -= 1

    except KeyboardInterrupt:
        tlog("KeyboardInterrupt")
        pass

    tlog("got no data :-( giving up")

    return []


@timeit
async def calculate_trends(pool: Pool) -> bool:
    # load snapshot
    with requests.Session() as session:
        url = (
            "https://api.polygon.io/"
            + "v2/snapshot/locale/us/markets/stocks/tickers"
        )
        with session.get(
            url,
            params={"apiKey": get_polygon_credentials(config.prod_api_key_id)},
        ) as response:
            if (
                response.status_code == 200
                and (r := response.json())["status"] == "OK"
            ):
                for ticker in r["tickers"]:
                    trading_data.snapshot[ticker.ticker] = TickerSnapshot(
                        symbol=ticker["ticker"],
                        volume=ticker["day"]["volume"],
                        today_change=ticker["todaysChangePerc"],
                    )

                if not trading_data.snapshot:
                    tlog("calculate_trends(): market snapshot not available")
                    return False

                # calculate sector trends
                sectors = await get_market_industries(pool)

                sector_tickers = {}
                for sector in sectors:
                    sector_tickers[sector] = await get_sector_tickers(
                        pool, sector
                    )

                    sector_volume = 0
                    adjusted_sum = 0.0
                    for symbol in sector_tickers[sector]:
                        sector_volume += trading_data.snapshot[symbol].volume
                        adjusted_sum += (
                            trading_data.snapshot[symbol].volume
                            * trading_data.snapshot[symbol].today_change
                        )

                    trading_data.sector_trend[sector] = round(
                        adjusted_sum / sector_volume, 2
                    )

                # calculate industry
                industries = await get_market_industries(pool)

                industry_tickers = {}
                for industry in industries:
                    industry_tickers[industry] = await get_industry_tickers(
                        pool, industry
                    )

                    industry_volume = 0
                    adjusted_sum = 0.0
                    for symbol in industry_tickers[sector]:
                        industry_volume += trading_data.snapshot[symbol].volume
                        adjusted_sum += (
                            trading_data.snapshot[symbol].volume
                            * trading_data.snapshot[symbol].today_change
                        )

                    trading_data.industry_trend[industry] = round(
                        adjusted_sum / industry_volume, 2
                    )

                return True

    return False


async def get_sector_tickers(pool: Pool, sector: str) -> List[str]:
    async with pool.acquire() as conn:
        async with conn.transaction():
            records = await conn.fetch(
                """
                    SELECT symbol
                    FROM ticker_data
                    WHERE sector = $1
                """,
                sector,
            )

            return [record[0] for record in records if record[0]]


async def get_industry_tickers(pool: Pool, industry: str) -> List[str]:
    async with pool.acquire() as conn:
        async with conn.transaction():
            records = await conn.fetch(
                """
                    SELECT symbol
                    FROM ticker_data
                    WHERE industry = $1
                """,
                industry,
            )

            return [record[0] for record in records if record[0]]


async def get_market_sectors(pool: Pool) -> List[str]:
    async with pool.acquire() as conn:
        async with conn.transaction():
            records = await conn.fetch(
                """
                    SELECT DISTINCT sector
                    FROM ticker_data
                """
            )
            return [record[0] for record in records if record[0]]


async def get_market_industries(pool: Pool) -> List[str]:
    async with pool.acquire() as conn:
        async with conn.transaction():
            records = await conn.fetch(
                """
                    SELECT DISTINCT industry
                    FROM ticker_data
                """
            )

            return [record[0] for record in records if record[0]]
