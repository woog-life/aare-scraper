import inspect
import logging
import os
import socket
import sys
from datetime import datetime
from typing import Tuple, Optional, Callable, Union, NewType, List

import pytz
import requests
import urllib3
from bs4 import BeautifulSoup, Tag
from telegram import Bot

TEMPERATURE_URL = "https://www.aare-bern.ch/wasserdaten-temperatur/"
# noinspection HttpUrlsUsage
# cluster internal communication
BACKEND_URL = os.getenv("BACKEND_URL") or "http://api:80"
BACKEND_PATH = os.getenv("BACKEND_PATH") or "lake/{}/temperature"
UUID = os.getenv("AARE_UUID")
API_KEY = os.getenv("API_KEY")

WATER_INFORMATION = NewType("WaterInformation", Tuple[str, float])


def create_logger(name: str, level: int = logging.DEBUG) -> logging.Logger:
    logger = logging.Logger(name)
    ch = logging.StreamHandler(sys.stdout)

    formatting = "[{}] %(asctime)s\t%(levelname)s\t%(module)s.%(funcName)s#%(lineno)d | %(message)s".format(name)
    formatter = logging.Formatter(formatting)
    ch.setFormatter(formatter)

    logger.addHandler(ch)
    logger.setLevel(level)

    return logger


def send_telegram_alert(message: str, token: str, chatlist: List[str]):
    logger = create_logger(inspect.currentframe().f_code.co_name)
    if not token:
        logger.error("TOKEN not defined in environment, skip sending telegram message")
        return

    if not chatlist:
        logger.error("chatlist is empty (env var: TELEGRAM_CHATLIST)")

    for user in chatlist:
        Bot(token=token).send_message(chat_id=user, text=f"Error while executing: {message}")


def get_website() -> Tuple[str, bool]:
    logger = create_logger(inspect.currentframe().f_code.co_name)
    url = TEMPERATURE_URL

    logger.debug(f"Requesting {url}")
    response = requests.get(url)

    content = response.content.decode("UTF-8")
    logger.debug(content)

    return content, True


def parse_website_xml(xml: str) -> BeautifulSoup:
    return BeautifulSoup(xml, "html.parser")


def extract_data(html: BeautifulSoup) -> Optional[Tuple[Tag, Tag]]:
    logger = create_logger(inspect.currentframe().f_code.co_name)

    temperature_tag = html.find("temp")
    if not temperature_tag:
        logger.error(f"<temp> not found in html {html}")
        return None

    timestamp_tag = html.find("temp-normal")
    if not temperature_tag:
        logger.error(f"<temp-normal> not found in html {html}")
        return None

    return (temperature_tag, timestamp_tag)


def get_tag_text_from_xml(xml: Union[BeautifulSoup, Tag], name: str, conversion: Callable) -> Optional:
    tag = xml.find(name)

    if not tag:
        return None

    return conversion(tag.text)


def get_water_information(soup: Tuple[Tag, Tag]) -> Optional[WATER_INFORMATION]:
    temperature_tag, timestamp_tag = soup

    time = datetime.strptime(timestamp_tag.text.strip(), "Letztes Update: %Y-%m-%d %H:%M:%S")
    local = pytz.timezone("Europe/Berlin")
    time = local.localize(time)
    iso_time = time.astimezone(pytz.utc).isoformat()

    temperature = float(temperature_tag.text.strip().split("°")[0])

    # noinspection PyTypeChecker
    # at this point pycharm doesn't think that the return type can be optional despite the many empty returns beforehand
    return iso_time, temperature


def send_data_to_backend(water_information: WATER_INFORMATION) -> Tuple[
    Optional[requests.Response], str]:
    logger = create_logger(inspect.currentframe().f_code.co_name)
    path = BACKEND_PATH.format(UUID)
    url = "/".join([BACKEND_URL, path])

    water_timestamp, water_temperature = water_information
    if water_temperature <= 0:
        return None, "water_temperature is <= 0, please approve this manually."

    headers = {"Authorization": f"Bearer {API_KEY}"}
    data = {"temperature": water_temperature, "time": water_timestamp}
    logger.debug(f"Send {data} to {url}")

    try:
        response = requests.put(url, json=data, headers=headers)
        logger.debug(f"success: {response.ok} | content: {response.content}")
    except (requests.exceptions.ConnectionError, socket.gaierror, urllib3.exceptions.MaxRetryError):
        logger.exception(f"Error while connecting to backend ({url})", exc_info=True)
        return None, url

    return response, url


def main() -> Tuple[bool, str]:
    if not UUID:
        root_logger.error("AARE_UUID not defined in environment")
        return False, "AARE_UUID not defined"
    elif not API_KEY:
        root_logger.error("API_KEY not defined in environment")
        return False, "API_KEY not defined"

    logger = create_logger(inspect.currentframe().f_code.co_name)
    content, success = get_website()
    if not success:
        message = f"Couldn't retrieve website: {content}"
        logger.error(message)
        return False, message

    soup = parse_website_xml(content)
    data = extract_data(soup)
    if not data:
        message = "Couldn't find a row with 'Wassertemperatur' as a description"
        logger.error(message)
        return False, message

    water_information = get_water_information(data)

    if not water_information:
        message = f"Couldn't retrieve water information from {soup}"
        logger.error(message)
        return False, message

    response, generated_backend_url = send_data_to_backend(water_information)

    if not response or not response.ok:
        message = f"Failed to put data ({water_information}) to backend: {generated_backend_url}\n{response.content}"
        logger.error(message)
        return False, message

    return True, ""


root_logger = create_logger("__main__")

success, message = main()
if not success:
    root_logger.error(f"Something went wrong ({message})")
    token = os.getenv("TOKEN")
    chatlist = os.getenv("TELEGRAM_CHATLIST") or "139656428"
    send_telegram_alert(message, token=token, chatlist=chatlist.split(","))
    sys.exit(1)
