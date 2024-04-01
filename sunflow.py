### Sunflow Cryptobot ###
#
# File that drives it all! 

# Load external libraries
from time import sleep
from pybit.unified_trading import WebSocket
from requests.exceptions import ChunkedEncodingError
from urllib3.exceptions import ProtocolError
from http.client import RemoteDisconnected
import traceback

# Load internal libraries
import config, defs, preload, indicators, trailing, orders

### Initialize variables ###

# Set default values
debug                     = config.debug                   # Debug
symbol                    = config.symbol                  # Symbol bot used for trading
klines                    = {}                             # KLines for symbol
interval                  = config.interval                # KLines timeframe interval 
limit                     = config.limit                   # Number of klines downloaded, used for calculcating technical indicators
ticker                    = {}                             # Ticker data, including lastPrice and time
info                      = {}                             # Instrument info on symbol
spot                      = 0                              # Spot price, always equal to lastPrice
profit                    = config.profit                  # Minimum profit percentage
depth                     = config.depth                   # Depth in percentages used to calculate market depth from orderbook
multiplier                = config.multiplier              # Multiply minimum order quantity by this

# Minimum spread between historical buy orders
use_spread                = {}                             # Spread
use_spread['enabled']     = config.spread_enabled          # Use spread
use_spread['distance']    = config.spread_distance         # Minimum spread in percentages

# Technical indicators
use_indicators            = {}                             # Technical indicators
use_indicators['enabled'] = config.indicators_enabled      # Use technical indicators
use_indicators['minimum'] = config.indicators_minimum      # Minimum advice value
use_indicators['maximum'] = config.indicators_maximum      # Maximum advice value

# Trailing order
active_order              = {}                              # Trailing order data
active_order['side']      = ""                              # Trailing buy
active_order['active']    = False                           # Trailing order active or not
active_order['start']     = 0                               # Start price when trailing order began     
active_order['previous']  = 0                               # Previous price
active_order['current']   = 0                               # Current price
active_order['distance']  = config.distance                 # Trigger price distance percentage
active_order['orderid']   = 0                               # Orderid
active_order['trigger']   = 0                               # Trigger price for order
active_order['wiggle']    = True                            # Make the distance dynamical
active_order['qty']       = 0                               # Order quantity

# Databases for buy and sell orders
all_buys                  = {}                              # All buys retreived from database file buy orders
all_sells                 = {}                              # Sell order linked to database with all buys orders

# Websockets where ticker is always on
ws_kline                      = True                        # Use klines websocket
ws_orderbook                  = False                       # Use orderbook websocket

### Functions ###

# Handle messages to keep tickers up to date
def handle_ticker(message):
    
    # Errors are not reported within websocket
    try:
   
        # Declare some variables global
        global spot, ticker, active_order, all_buys, all_sells

        # Show incoming message
        if debug:
            print(defs.now_utc()[1] + "Sunflow: handle_ticker: *** Incoming ticker ***")
            print(str(message) + "\n")

        # Get ticker
        ticker              = {}
        ticker['time']      = int(message['ts'])
        ticker['lastPrice'] = float(message['data']['lastPrice'])

        # Has price changed, then run all kinds of actions
        if spot != ticker['lastPrice']:
            print(defs.now_utc()[1] + "Sunflow: handle_ticker: lastPrice changed from " + str(spot) + " " + info['quoteCoin'] + " to " + str(ticker['lastPrice']) + " " + info['quoteCoin'] + "\n")
                
            # Run trailing if active
            if active_order['active']:
                active_order['current'] = spot
                active_order['status']  = 'Trailing'
                trail_results = trailing.trail(symbol, active_order, info, all_buys, all_sells)
                active_order = trail_results[0]
                all_buys     = trail_results[1]

            # Check if and how much we can sell
            check_sell_results = orders.check_sell(spot, profit, active_order['distance'], all_buys)
            all_sells = check_sell_results[0]
            qty       = check_sell_results[1]
            can_sell  = check_sell_results[2]
            
            # Adjust quantity to exchange regulations
            qty = defs.precision(qty, info['basePrecision'])

            # Initiate first sell
            if not active_order['active'] and can_sell:
                active_order = orders.sell(symbol, spot, qty, active_order, info)

            # Amend existing sell trailing order if required
            if active_order['active'] and active_order['side'] == "Sell":

                # Only amend order if the quantity to be sold has changed
                if debug:
                    print(defs.now_utc()[1] + "Sunflow: handle_ticker: qty = " + str(qty) + ", active_order['qty'] = " + str(active_order['qty']))
                if qty != active_order['qty'] and qty > 0:
                    trailing.amend_sell(symbol, active_order['orderid'], qty, info)
                    active_order['qty'] = qty

        # Always set new spot price
        spot = ticker['lastPrice']

    # Report error
    except Exception as e:
        tb_info = traceback.extract_tb(e.__traceback__)
        filename, line, func, text = tb_info[-1]
        print(defs.now_utc()[1] + f"Sunflow: handle_ticker: An error occurred in {filename} on line {line}: {e}")
        print("Full traceback:")
        traceback.print_tb(e.__traceback__)

# Handle messages to keep klines up to date
def handle_kline(message):

    # Errors are not reported within websocket
    try:

        # Declare some variables global
        global klines, active_order, all_buys

        # Initialize variables
        kline = {}
     
        # Show incoming message
        if debug:
            print(defs.now_utc()[1] + "Sunflow: handle_kline: *** Incoming kline ***")
            print(message)

        # Get newest kline
        kline['time']     = int(message['data'][0]['start'])
        kline['open']     = float(message['data'][0]['open'])
        kline['high']     = float(message['data'][0]['high'])
        kline['low']      = float(message['data'][0]['low'])
        kline['close']    = float(message['data'][0]['close'])
        kline['volume']   = float(message['data'][0]['volume'])
        kline['turnover'] = float(message['data'][0]['turnover'])

        # Check if we have a finished kline
        if message['data'][0]['confirm'] == True:

            # Add new kline and remove the last
            print(defs.now_utc()[1] + "Sunflow: handle_kline: Adding new kline to klines\n")
            klines = defs.new_kline(kline, klines)
      
        else:            
            # Remove the first kline and replace with fresh kline
            klines = defs.update_kline(kline, klines)       
        
        # Only initiate buy and do complex calculations when not already trailing
        if not active_order['active']:
            
            # Initialize variables
            advice_indicators = advice_spread = False

            # Check technical indicators for buy decission
            if use_indicators['enabled']:
                technical_indicators = indicators.calculate(klines, spot)
                technical_advice     = indicators.advice(technical_indicators)
                if (technical_advice[0] > use_indicators['minimum']) and (technical_advice[0] < use_indicators['maximum']):
                    advice_indicators = True
            else:
                advice_indicators = True
            
            # Check spread for buy decission
            if use_spread['enabled']:
                check_spread_advice = defs.check_spread(all_buys, spot, use_spread['distance'])
                advice_spread = check_spread_advice[0]
                advice_near   = round(check_spread_advice[1], 2)
            else:
                advice_spread = True
            
            # Check orderbook for buy decission
            # *** CHECK *** To be implemented
            
            # Combine all data to make a buy decission
            print(defs.now_utc()[1] + "Sunflow: handle_kline: Buy matrix: Indicators: " + str(advice_indicators) + " (" + str(technical_advice[0]) + ") | Spread: " + str(advice_spread) + " (" + str(advice_near) + "%)\n")
            if (advice_indicators) and (advice_spread):
                buy_result   = orders.buy(symbol, spot, active_order, all_buys, info)
                active_order = buy_result[0]
                all_buys     = buy_result[1]

    # Report error
    except Exception as e:
        tb_info = traceback.extract_tb(e.__traceback__)
        filename, line, func, text = tb_info[-1]
        print(defs.now_utc()[1] + f"An error occurred in {filename} on line {line}: {e}")
        print("Full traceback:")
        traceback.print_tb(e.__traceback__)
    
# Handle messages to keep orderbook up to date
def handle_orderbook(message):

    # Errors are not reported within websocket
    try:
      
        # Show incoming message
        if debug:
            print(defs.now_utc()[1] + "Sunflow: handle_orderbook: *** Incoming orderbook ***")
            print(message)
        
        # Recalculate depth to numerical value
        depthN = ((2 * depth) / 100) * spot
        
        # Extracting bid (buy) and ask (sell) arrays
        bids = message['data']['b']
        asks = message['data']['a']

        # Initialize total quantities within depth for buy and sell
        total_buy_within_depth  = 0
        total_sell_within_depth = 0

        # Calculate total buy quantity within depth
        for bid in bids:
            price, quantity = float(bid[0]), float(bid[1])
            if (spot - depthN) <= price <= spot:
                total_buy_within_depth += quantity

        # Calculate total sell quantity within depth
        for ask in asks:
            price, quantity = float(ask[0]), float(ask[1])
            if spot <= price <= (spot + depthN):
                total_sell_within_depth += quantity

        # Calculate total quantity (buy + sell)
        total_quantity_within_depth = total_buy_within_depth + total_sell_within_depth

        # Calculate percentages
        buy_percentage = (total_buy_within_depth / total_quantity_within_depth) * 100 if total_quantity_within_depth > 0 else 0
        sell_percentage = (total_sell_within_depth / total_quantity_within_depth) * 100 if total_quantity_within_depth > 0 else 0

        # Output the stdout
        if debug:        
            print(defs.now_utc()[1] + "Sunflow: handle_orderbook: Orderbook")
            print(f"Spot price         : {spot}")
            print(f"Lower depth       : {spot - depth}")
            print(f"Upper depth       : {spot + depth}\n")

            print(f"Total Buy quantity : {total_buy_within_depth}")
            print(f"Total Sell quantity: {total_sell_within_depth}")
            print(f"Total quantity     : {total_quantity_within_depth}\n")

            print(f"Buy within depth  : {buy_percentage:.2f}%")
            print(f"Sell within depth : {sell_percentage:.2f}%")

        print(defs.now_utc()[1] + f"Sunflow: handle_orderbook: Orderbook: Market depth (Buy / Sell | depth (Advice)): {buy_percentage:.2f}% / {sell_percentage:.2f}% | {depth}% ", end="")
        if buy_percentage >= sell_percentage:
            print("(BUY)\n")
        else:
            print("(SELL)\n")

    # Report error
    except Exception as e:
        tb_info = traceback.extract_tb(e.__traceback__)
        filename, line, func, text = tb_info[-1]
        print(defs.now_utc()[1] + f"An error occurred in {filename} on line {line}: {e}")
        print("Full traceback:")
        traceback.print_tb(e.__traceback__)


### Start main program ###

# Welcome screen
print("\n*************************")
print("*** Sunflow Cryptobot ***")
print("*************************\n")
print("Symbol  : " + symbol)
print("Interval: " + str(interval) + "m")
print("Limit   : " + str(limit))
print()

# Preload all requirements
print("*** Preloading ***")

preload.check_files()
klines   = preload.get_klines(symbol, interval, limit)
ticker   = preload.get_ticker(symbol)
spot     = ticker['lastPrice']
info     = preload.get_info(symbol, spot, multiplier)
all_buys = preload.get_buys(config.dbase_file) 
preload.check_orders(all_buys)

print("*** Starting ***\n")


### Websockets ###

def connect_websocket():
    ws = WebSocket(testnet=False, channel_type="spot")
    return ws

def subscribe_streams(ws):
    # Continuously get tickers from websocket
    ws.ticker_stream(symbol=symbol, callback=handle_ticker)

    # At request get klines from websocket
    if ws_kline:
        ws.kline_stream(interval=1, symbol=symbol, callback=handle_kline)

    # At request get orderbook from websocket
    if ws_orderbook:
        ws.orderbook_stream(depth=50, symbol=symbol, callback=handle_orderbook)

def main():
    ws = connect_websocket()
    subscribe_streams(ws)

    while True:
        try:
            sleep(1)  # Your processing logic here
        except (RemoteDisconnected, ProtocolError, ChunkedEncodingError) as e:
            print(defs.now_utc()[1] + f"Connection lost. Reconnecting due to: {e}")
            sleep(5)  # Wait a bit before reconnecting to avoid hammering the server
            ws = connect_websocket()
            subscribe_streams(ws)

if __name__ == "__main__":
    main()