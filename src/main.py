from flask import Flask, jsonify, request, render_template
import requests
import asyncio
import websockets
import sys
import os
import json
import uuid
import threading
import random
import time
from implied_volatility import implied_vol
from datetime import datetime


# TODO: serve static files via nginx
app = Flask(__name__)
app.json_provider_class.compact = False

BASE_ASSET_CODE = 'SiH4'
STRIKES_COUNT = 11
STRIKE_STEP = 1000
MOEX_OPTIONS_LIST_URL = 'https://iss.moex.com/iss/statistics/engines/futures/markets/options/series/Si-3.24M210324XA/securities.json'
ALOR_REFRESH_TOKEN_URL = 'https://oauth.alor.ru/refresh'
ALOR_WS_URL = 'wss://api.alor.ru/ws'

g_alor_auth = {
    'token': ''
}
g_model = {
    'base_asset': {
        'quotes': {}
    },
    'options': {}
}
g_async_queue = asyncio.Queue()


def get_guid_dict():
    guid = str(uuid.uuid4())
    return guid, {'guid': guid, 'data': {}}


@app.route('/model', methods=['GET'])
def get_model():
    return jsonify(g_model)


@app.route('/chart.json', methods=['GET'])
def get_diagram_data():
    return jsonify(retrieve_data_for_diagram())


def get_time_to_option_maturity():
    # TODO: учитывать точность до минут, когда даты экспирации опционов будут зависеть от серии
    options_expiration_date = datetime(2024, 3, 21, 18, 50)

    difference = options_expiration_date - datetime.now()
    seconds_in_year = 365 * 24 * 60 * 60
    return difference.total_seconds() / seconds_in_year


def get_iv_for_option_price(asset_price, strike_price, opt_price, option_type):
    # parameters
    S = asset_price
    K = strike_price
    T = get_time_to_option_maturity()
    r = 0  # risk-free interest rate
    C = opt_price

    tol = 10 ** -8
    iv = implied_vol(C, S, K, r, T, tol, option_type)
    if not iv:
        return None
    return iv * 100


def populate_options_dict(response_data):
    securities_columns = response_data['securities']['columns']
    securities_data = response_data['securities']['data']
    options_dict = {}
    for security_data in securities_data:
        security_dict = {}

        for i in range(len(securities_columns)):
            key = securities_columns[i]
            value = security_data[i]
            security_dict[key] = value

        is_traded = security_dict['is_traded']
        if is_traded:
            strike = security_dict['strike']
            option_type = security_dict['option_type']
            if strike not in options_dict:
                options_dict[strike] = {}

            options_dict[strike][option_type] = {'moex_data': security_dict, 'volatilities': {}}
    return options_dict


def get_options_from_moex():
    response_object = get_object_from_json_endpoint(MOEX_OPTIONS_LIST_URL)
    if not response_object:
        return None

    return populate_options_dict(response_object)


def get_env_or_exit(var_name):
    value = os.environ.get(var_name)

    if value is None:
        print_error_message_and_exit(f'{var_name} environment variable is not set.')

    return value


def get_alor_authorization_token():
    alor_client_token = get_env_or_exit('ALOR_CLIENT_TOKEN')
    params = {'token': alor_client_token}

    response = get_object_from_json_endpoint(ALOR_REFRESH_TOKEN_URL, 'POST', params)
    alor_authorization_token = ''
    if response:
        alor_authorization_token = response['AccessToken']
    return alor_authorization_token


def get_object_from_json_endpoint(url, method='GET', params={}):
    response = requests.request(method, url, params=params)

    response_data = None
    # Check if the request was successful (status code 200)
    if response.status_code == 200:
        # Print the response content (JSON data)
        response_data = response.json()
    else:
        # Print an error message if the request was not successful
        print_error_message_and_exit(f"Error: {response.status_code}")
    return response_data


def print_error_message_and_exit(error_message):
    sys.stderr.write(error_message + '\n')
    sys.exit(1)


# Центральный страйк - наиболее близкий к цене базового актива с учётом заданного шага цены страйков
def calculate_central_strike(base_asset_price):
    return round(base_asset_price / STRIKE_STEP) * STRIKE_STEP


# Формируем список страйков с учетом заданного количества страйков, шага цены страйка и центрального страйка
# TODO: проверять, что все страйки больше нуля
def get_list_of_strikes(central_strike):
    strikes_before_count = STRIKES_COUNT // 2
    first_strike = central_strike - strikes_before_count * STRIKE_STEP
    strikes = []
    for i in range(STRIKES_COUNT):
        strikes.append(first_strike + i * STRIKE_STEP)

    return strikes


def subscribe_to_option_instrument(option_from_model):
    guid, dict = get_guid_dict()
    option_from_model['instrument'] = dict
    asset_code = option_from_model['moex_data']['secid']
    instrument_subscribe_json = get_json_to_instrument_subscribe(asset_code, guid)
    g_async_queue.put_nowait(instrument_subscribe_json)


def subscribe_to_option_quotes(option_from_model):
    guid, dict = get_guid_dict()
    option_from_model['quotes'] = dict
    asset_code = option_from_model['moex_data']['secid']
    quotes_subscribe_json = get_json_to_quotes_subscribe(asset_code, guid)
    g_async_queue.put_nowait(quotes_subscribe_json)


def subscribe_to_options_data(list_of_strikes):
    for strike in list_of_strikes:
        for option in g_model['options'][strike].values():
            if 'quotes' not in option:
                subscribe_to_option_quotes(option)
            if 'instrument' not in option:
                subscribe_to_option_instrument(option)


def handle_alor_data(guid, data):
    base_asset_quotes = g_model['base_asset']['quotes']
    if base_asset_quotes['guid'] == guid:
        handle_base_asset_quotes_event(base_asset_quotes, data)
    else:
        for strike, options in g_model['options'].items():
            for option_type, option in options.items():
                if 'quotes' in option and option['quotes']['guid'] == guid:
                    handle_option_quotes_event(strike, option_type, option, data)
                elif 'instrument' in option and option['instrument']['guid'] == guid:
                    option['instrument']['data'] = data


def handle_base_asset_quotes_event(base_asset_quotes, new_quotes_data):
    prev_last_price = None
    if 'last_price' in base_asset_quotes['data']:
        prev_last_price = base_asset_quotes['data']['last_price']
    base_asset_quotes['data'] = new_quotes_data

    last_price = new_quotes_data['last_price']
    update_list_of_strikes(last_price)

    if prev_last_price is not None and prev_last_price != last_price:
        # TODO: remove debug message
        print(f'Last price changed! Prev last price: {prev_base_asset_quotes_data['last_price']}, now last price: {last_price}')
        recalculate_volatilities()


def handle_option_quotes_event(strike, option_type, option, new_quotes):
    base_asset_last_price = g_model['base_asset']['quotes']['data']['last_price']
    prev_quotes = option['quotes']['data']
    if 'last_price' not in prev_quotes or prev_quotes['last_price'] != new_quotes['last_price']:
        # Волатильность по цене последней сделки опциона вычисляется только по факту изменения,
        # так как это уже свершившиеся событие, и волатильность по нему не нужно пересчитывать постоянно
        option_last_price = new_quotes['last_price']
        option['volatilities']['last_price_volatility'] = get_iv_for_option_price(base_asset_last_price, strike,
                                                                                  option_last_price, option_type)

    option['volatilities']['ask_volatility'] = get_iv_for_option_price(base_asset_last_price, strike, new_quotes['ask'],
                                                                       option_type)
    option['volatilities']['bid_volatility'] = get_iv_for_option_price(base_asset_last_price, strike, new_quotes['bid'],
                                                                       option_type)

    option['quotes']['data'] = new_quotes


def update_list_of_strikes(base_asset_last_price):
    central_strike = calculate_central_strike(base_asset_last_price)
    list_of_strikes = get_list_of_strikes(central_strike)
    g_model['list_of_strikes'] = list_of_strikes
    subscribe_to_options_data(list_of_strikes)


def recalculate_volatilities():
    if 'data' not in g_model['base_asset']['quotes'] or 'list_of_strikes' not in g_model:
        return

    base_asset_last_price = g_model['base_asset']['quotes']['data']['last_price']
    for strike in g_model['list_of_strikes']:
        options = g_model['options'][strike]
        for option_type, option in options.items():
            if 'quotes' in option and 'data' in option['quotes']:
                quotes = option['quotes']['data']
                option['volatilities']['ask_volatility'] = get_iv_for_option_price(base_asset_last_price, strike, quotes['ask'],
                                                                                   option_type)
                option['volatilities']['bid_volatility'] = get_iv_for_option_price(base_asset_last_price, strike, quotes['bid'],
                                                                                   option_type)


def retrieve_data_for_diagram():
    strikes_data = []
    last_price = g_model['base_asset']['quotes']['data']['last_price']
    for strike in g_model['list_of_strikes']:
        call_option_data = g_model['options'][strike]['C']
        put_option_data = g_model['options'][strike]['P']
        volatility = call_option_data['instrument']['data']['volatility']

        strikes_data.append({
            'strike': strike,
            'volatility': volatility,
            'call': call_option_data['volatilities'],
            'put': put_option_data['volatilities'],
        })

    return {
        'strikes': strikes_data,
        'last_price': last_price,
    }


async def consumer(message):
    message_dict = json.loads(message)
    if 'data' in message_dict and 'guid' in message_dict:
        guid = message_dict['guid']
        data = message_dict['data']
        handle_alor_data(guid, data)


async def consumer_handler(websocket):
    async for message in websocket:
        await consumer(message)


async def producer_handler(websocket):
    while True:
        message = await g_async_queue.get()
        await websocket.send(message)


async def handler(websocket):
    consumer_task = asyncio.create_task(consumer_handler(websocket))
    producer_task = asyncio.create_task(producer_handler(websocket))
    done, pending = await asyncio.wait(
        [consumer_task, producer_task],
        return_when=asyncio.FIRST_COMPLETED,
    )
    for task in pending:
        task.cancel()


async def connect_to_alor_websocket():
    async with websockets.connect(ALOR_WS_URL) as websocket:
        await handler(websocket)


def get_json_to_quotes_subscribe(asset_code, guid):
    request_data = {
        "opcode": "QuotesSubscribe",
        "code": asset_code,
        "exchange": "MOEX",
        "guid": guid,
        "token": g_alor_auth['token']
    }
    return json.dumps(request_data)


def get_json_to_instrument_subscribe(asset_code, guid):
    request_data = {
        "opcode": "InstrumentsGetAndSubscribeV2",
        "code": asset_code,
        "exchange": "MOEX",
        "guid": guid,
        "token": g_alor_auth['token']
    }
    return json.dumps(request_data)


def run_flask_app():
    # Enable pretty-printing for JSON responses
    app.run(host='0.0.0.0', port=5000)


def call_function_with_timeout():
    while True:
        recalculate_volatilities()
        time.sleep(1)


def start_flask_thread():
    # Start Flask app in a separate thread
    flask_thread = threading.Thread(target=run_flask_app)
    flask_thread.daemon = True
    flask_thread.start()


def start_thread_with_timeout():
    # Create a new thread that calls the function every second
    thread = threading.Thread(target=call_function_with_timeout)
    thread.daemon = True
    thread.start()


def subscribe_to_exchange_events():
    g_model['options'] = get_options_from_moex()
    g_alor_auth['token'] = get_alor_authorization_token()

    base_asset_quotes_guid, base_asset_quotes_dict = get_guid_dict()
    g_model['base_asset']['quotes'] = base_asset_quotes_dict
    quotes_subscribe_to_base_asset_json = get_json_to_quotes_subscribe(BASE_ASSET_CODE, base_asset_quotes_guid)
    g_async_queue.put_nowait(quotes_subscribe_to_base_asset_json)

    asyncio.run(connect_to_alor_websocket(), debug=True)


def main():
    start_flask_thread()
    start_thread_with_timeout()
    subscribe_to_exchange_events()


if __name__ == '__main__':
    main()
