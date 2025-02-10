from infrastructure import env_utils
from infrastructure.alor_api import AlorApi
from infrastructure.moex_api import get_futures_series

asset_list = ('RTS','Si')

class AlorApiTest:

    def __init__(self):
        print('AlorApiTest')
        alor_client_token = env_utils.get_env_or_exit('ALOR_CLIENT_TOKEN')
        self._alorApi = AlorApi(alor_client_token)

    def run(self):
        self._test_subscribe_to_quotes()
        self._alorApi.run_async_connection(False)

    def _test_subscribe_to_quotes(self):
        print('\n _test_subscribe_to_quotes')
        for ticker in list_futures_all:
            self._alorApi.subscribe_to_quotes(ticker, self._handle_quotes_event)
        self._alorApi.subscribe_to_quotes('RI97500BB5B', self._handle_quotes_event)
        self._alorApi.subscribe_to_instrument('RI97500BB5B', self._handle_option_instrument_event)

    def _handle_quotes_event(self, ticker, data):
        print(ticker, 'last_price:', data['last_price'], 'last_price_timestamp:', data['last_price_timestamp'], 'bid:', data['bid'], 'ask:', data['ask'])

    def _handle_option_instrument_event(self, ticker, data):
        print(ticker, 'theorPrice:', data['theorPrice'], 'volatility:', data['volatility'])

# Две ближайшие (текущая и следующая) фьючерсные серии по базовому активу из списка asset_list
list_futures_current = []
list_futures_all = []
for asset_code in asset_list: # Пробегаемся по списку активов
    data_fut = get_futures_series(asset_code)
    info_fut_1 = data_fut[len(data_fut) - 1]
    ticker = info_fut_1['secid']
    list_futures_current.append(info_fut_1['secid'])
    list_futures_all.append(info_fut_1['secid'])
    info_fut_2 = data_fut[len(data_fut) - 2]
    list_futures_all.append(info_fut_2['secid'])
# print('\n list_futures_current', '\n', list_futures_current) # Фьючерсы текущей серии
print('list_futures_all', '\n', list_futures_all) # Фьючерсы текущей и следующей серии