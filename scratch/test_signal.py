from brain.markets import MarketClient
from brain.weather import WeatherClient
import json
import sys
import unittest

# Ensure UTF-8 output even on Windows if possible, but we'll remove emojis just in case
@unittest.skip("Requires live network access to Polymarket and weather APIs")
def test_live_capture():
    print("--- SIGNAL CATCHING TEST START ---")
    
    market_client = MarketClient()
    weather_client = WeatherClient()
    
    # 1. Fetch real active markets
    print("[1/3] Fetching active weather markets from Polymarket...")
    markets = market_client.get_weather_markets()
    
    if not markets:
        print("FAIL: No active weather markets found right now.")
        return

    # 2. Pick the first valid market to test
    target_market = None
    location_data = None
    
    for m in markets:
        question = m.get('question', '')
        location_data = market_client.parse_market_location(question)
        if location_data:
            target_market = m
            break
            
    if not target_market:
        print("FAIL: Found markets, but couldn't parse a known city from descriptions.")
        if markets:
            print(f"Sample Question: {markets[0].get('question')}")
        return

    lat = location_data.get("lat")
    lon = location_data.get("lon")
    is_us = location_data.get("is_us")
    print(f"[2/3] Successfully parsed market location:")
    print(f"      Question: {target_market['question']}")
    print(f"      Coordinates: {lat}, {lon} (US: {is_us})")

    # 3. Fetch real weather data
    print(f"[3/3] Fetching live forecast from {'NOAA' if is_us else 'Open-Meteo'}...")
    forecast = weather_client.get_forecast(lat, lon, is_us=is_us)
    
    if forecast:
        # Get the first temp sample
        try:
            if is_us:
                # NOAA hourly format
                current_temp = forecast[0]['temperature']
                unit = forecast[0]['temperatureUnit']
            else:
                # Open Meteo format
                current_temp = forecast['temperature_2m'][0]
                unit = "C"
                
            print(f"SUCCESS! Live Signal Caught.")
            print(f"      Location: Detected")
            print(f"      Forecast Temp: {current_temp} {unit}")
            print("--- TEST COMPLETE ---")
        except Exception as e:
            print(f"FAIL: Error parsing forecast data: {e}")
    else:
        print("FAIL: Failed to fetch weather data for the location.")

if __name__ == "__main__":
    test_live_capture()
