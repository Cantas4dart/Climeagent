import requests
import re
import time
from requests.adapters import HTTPAdapter
from requests.exceptions import RequestException, SSLError
from urllib3.util.retry import Retry

class MarketClient:
    def __init__(self):
        self.gamma_api_url = "https://gamma-api.polymarket.com"
        self.events_api_url = f"{self.gamma_api_url}/events"
        self.markets_api_url = f"{self.gamma_api_url}/markets"
        self.tags_api_url = f"{self.gamma_api_url}/tags"
        self.geocode_cache = {}  # Cache geocoding results to avoid repeated API calls
        self.tag_id_cache = {}
        self.session = self._build_session()

        # This bot is intentionally temperature-only, not a general weather-market bot.
        self.temperature_keywords = [
            "highest temperature",
            "lowest temperature",
            "temperature",
            "degrees",
            "celsius",
            "fahrenheit",
            "high temp",
            "low temp",
        ]
        self.search_terms = [
            "temperature",
            "highest temperature",
            "lowest temperature",
        ]
        self.temperature_tag_keywords = [
            "temperature",
            "high temp",
            "high-temp",
            "low temp",
            "low-temp",
        ]
        self.weather_tag_keywords = [
            "weather",
            "climate",
        ]
        self.blocked_country_codes = {
            "DZ", "AO", "BJ", "BW", "BF", "BI", "CM", "CV", "CF", "TD", "KM", "CD", "CG",
            "CI", "DJ", "EG", "GQ", "ER", "SZ", "ET", "GA", "GM", "GH", "GN", "GW", "KE",
            "LS", "LR", "LY", "MG", "MW", "ML", "MR", "MU", "YT", "MA", "MZ", "NA", "NE",
            "NG", "RE", "RW", "SH", "ST", "SN", "SC", "SL", "SO", "ZA", "SS", "SD", "TZ",
            "TG", "TN", "UG", "EH", "ZM", "ZW",
        }

    def _build_session(self):
        session = requests.Session()
        retry = Retry(
            total=3,
            connect=3,
            read=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session

    def get_weather_markets(self):
        """
        Fetch active city daily temperature ladder markets from Gamma API.

        Uses both tag/event discovery and direct keyword search so terminal
        results line up more closely with Polymarket's site search.
        """
        active_events = self._fetch_active_events()
        tagged_events = self._fetch_tagged_events()
        search_markets = self._fetch_search_markets()
        if not active_events and not search_markets:
            print("[MARKETS] WARNING: No active events or search markets returned from Gamma API.")
            return []

        unique_markets = {}
        reviewed_markets = 0

        combined_events = {}
        for event in active_events + tagged_events:
            event_id = event.get("id")
            if event_id is None:
                continue
            combined_events[event_id] = event

        for event in combined_events.values():
            for market in event.get("markets", []):
                reviewed_markets += 1

                if not self._is_tradeable_market(market):
                    continue

                if not self._is_temperature_market(market, event):
                    continue

                market_id = market.get("id")
                if not market_id:
                    continue

                market["_display_question"] = self._market_display_name(market, event)
                unique_markets[market_id] = market

        for market in search_markets:
            reviewed_markets += 1

            if not self._is_tradeable_market(market):
                continue

            if not self._is_temperature_market(market, None):
                continue

            market_id = market.get("id")
            if not market_id:
                continue

            market["_display_question"] = self._market_display_name(market)
            unique_markets[market_id] = market

        temperature_markets = list(unique_markets.values())
        print(f"[MARKETS] Reviewed {reviewed_markets} candidate markets across {len(combined_events)} events and {len(search_markets)} search hits.")
        print(f"[MARKETS] Final active city temperature rung markets: {len(temperature_markets)}")

        if len(temperature_markets) == 0:
            print("[MARKETS] WARNING: No active city temperature ladders found on Polymarket right now.")

        return temperature_markets

    def _fetch_active_events(self, page_limit=100, max_pages=20):
        """Fetch active events with pagination, following Polymarket's recommended discovery flow."""
        events = []
        offset = 0

        for page in range(max_pages):
            try:
                params = {
                    "active": "true",
                    "closed": "false",
                    "order": "volume_24hr",
                    "ascending": "false",
                    "limit": page_limit,
                    "offset": offset,
                }
                batch = self._get_json(self.events_api_url, params=params, timeout=20, label=f"Events page {page + 1}")
                if not isinstance(batch, list):
                    print("[MARKETS] Unexpected events payload from Gamma API.")
                    break

                print(f"[MARKETS] Events page {page + 1} -> {len(batch)} events")
                if len(batch) == 0:
                    break

                events.extend(batch)
                if len(batch) < page_limit:
                    break

                offset += page_limit
            except Exception as e:
                print(f"[MARKETS] Events page {page + 1} FAILED: {e}")
                break

        return events

    def _fetch_tagged_events(self, page_limit=100):
        """Fetch events by Polymarket weather/temperature tags so category discovery is explicit."""
        events = []
        for slug in ("temperature", "weather"):
            tag_id = self._get_tag_id(slug)
            if not tag_id:
                continue

            try:
                params = {
                    "tag_id": tag_id,
                    "related_tags": "true",
                    "active": "true",
                    "closed": "false",
                    "limit": page_limit,
                    "offset": 0,
                }
                batch = self._get_json(self.events_api_url, params=params, timeout=20, label=f"Tag events '{slug}'")
                if not isinstance(batch, list):
                    print(f"[MARKETS] Tag events '{slug}' returned an unexpected payload.")
                    continue

                print(f"[MARKETS] Tag events '{slug}' -> {len(batch)} events")
                events.extend(batch)
            except Exception as e:
                print(f"[MARKETS] Tag events '{slug}' FAILED: {e}")

        return events

    def _fetch_search_markets(self, limit_per_term=100):
        """Fallback discovery path using the same search-style terms that surface markets on the site."""
        markets = []

        for term in self.search_terms:
            try:
                params = {
                    "active": "true",
                    "closed": "false",
                    "limit": limit_per_term,
                    "search": term,
                }
                batch = self._get_json(self.markets_api_url, params=params, timeout=20, label=f"Search '{term}'")
                if not isinstance(batch, list):
                    print(f"[MARKETS] Search '{term}' returned an unexpected payload.")
                    continue

                print(f"[MARKETS] Search '{term}' -> {len(batch)} raw markets")
                markets.extend(batch)
            except Exception as e:
                print(f"[MARKETS] Search '{term}' FAILED: {e}")

        return markets

    def _get_tag_id(self, slug):
        if slug in self.tag_id_cache:
            return self.tag_id_cache[slug]

        try:
            tag = self._get_json(f"{self.tags_api_url}/slug/{slug}", timeout=20, label=f"Tag '{slug}'")
            tag_id = tag.get("id") if isinstance(tag, dict) else None
            if tag_id:
                self.tag_id_cache[slug] = tag_id
                print(f"[MARKETS] Tag '{slug}' -> id {tag_id}")
                return tag_id
        except Exception as e:
            print(f"[MARKETS] Tag '{slug}' lookup FAILED: {e}")

        self.tag_id_cache[slug] = None
        return None

    def _get_json(self, url, params=None, timeout=20, label="request", attempts=3):
        last_error = None
        for attempt in range(1, attempts + 1):
            try:
                res = self.session.get(url, params=params, timeout=timeout)
                res.raise_for_status()
                return res.json()
            except SSLError as e:
                last_error = e
                if attempt < attempts:
                    print(f"[MARKETS] {label} SSL error on attempt {attempt}/{attempts}; retrying...")
                    time.sleep(attempt)
                    continue
                raise
            except RequestException as e:
                last_error = e
                if attempt < attempts:
                    print(f"[MARKETS] {label} request error on attempt {attempt}/{attempts}; retrying...")
                    time.sleep(attempt)
                    continue
                raise
            except ValueError as e:
                last_error = e
                raise

        if last_error:
            raise last_error

    def _is_tradeable_market(self, market):
        """Only consider live, order-book-enabled markets that can actually be traded."""
        if not market.get("active", False):
            return False
        if market.get("closed", True):
            return False
        if not market.get("enableOrderBook", False):
            return False

        accepting_orders = market.get("acceptingOrders")
        if accepting_orders is False:
            return False

        return True

    def _is_temperature_market(self, market, event=None):
        """Restrict discovery to city/day highest-temperature ladder rungs only."""
        question = market.get("question", "").lower()
        title = market.get("title", "").lower()
        group_item_title = market.get("groupItemTitle", "").lower()
        description = market.get("description", "").lower()
        slug = market.get("slug", "").lower()
        event_title = event.get("title", "").lower() if event else market.get("eventTitle", "").lower()
        event_slug = event.get("slug", "").lower() if event else market.get("eventSlug", "").lower()
        event_tag_text = self._extract_tag_text(event.get("tags", [])) if event else ""
        market_tag_text = self._extract_tag_text(market.get("tags", []))
        tag_text = " ".join([event_tag_text, market_tag_text]).strip()

        text_blob = " ".join([question, title, group_item_title, description, slug, event_title, event_slug, tag_text])
        has_temperature_keyword = any(keyword in text_blob for keyword in self.temperature_keywords)
        has_temperature_tag = any(keyword in tag_text for keyword in self.temperature_tag_keywords)
        has_weather_tag = any(keyword in tag_text for keyword in self.weather_tag_keywords)
        ladder_context = " ".join([event_title, title, group_item_title]).strip()
        has_city_temperature_event = bool(
            re.search(r'\bhighest temperature in .+ on [a-z]+ \d{1,2}\b', ladder_context)
        )
        looks_like_temperature_rung = bool(
            re.search(r'\bwill the highest temperature in\b', question)
            and re.search(r'\bon [a-z]+ \d{1,2}\b', question)
            and re.search(r'(?:between|or below|or higher|\d+(?:\.\d+)?\s*(?:°\s*)?(?:f|c))', question)
        )

        is_temperature = (
            has_city_temperature_event
            and looks_like_temperature_rung
            and has_temperature_keyword
            and (has_temperature_tag or has_weather_tag)
        )

        if is_temperature:
            print(f"[MARKETS]   [OK] KEPT: {self._market_display_name(market, event)[:120]}")

        return is_temperature

    def _extract_tag_text(self, tags):
        return " ".join(
            f"{tag.get('label', '')} {tag.get('slug', '')}".lower()
            for tag in tags
            if isinstance(tag, dict)
        )

    def _market_display_name(self, market, event=None):
        event_title = ""
        if event:
            event_title = event.get("title", "") or event.get("slug", "")
        if not event_title:
            event_title = market.get("eventTitle", "") or market.get("title", "") or market.get("groupItemTitle", "")

        question = market.get("question", "") or market.get("slug", "")
        if event_title and question and question.lower() != event_title.lower():
            return f"{event_title} :: {question}"
        return event_title or question or "Unknown market"

    def parse_market_location(self, market_description):
        """
        Resolve a city from market question to rich location metadata.

        Strategy:
        1. Check the hardcoded dictionary first (fastest, most reliable).
        2. If no match, attempt to extract a city name and geocode it via
           Open-Meteo's free geocoding API (no API key needed).
        3. Cache geocoding results to avoid repeated API calls.
        """

        # --- Step 1: Hardcoded high-priority locations ---
        locations = {
            # US Markets (is_us=True -> uses NOAA forecast)
            "New York": self._location_details("New York", 40.7128, -74.0060, True, "US", "United States", "North America", "America/New_York"),
            "NYC": self._location_details("New York", 40.7128, -74.0060, True, "US", "United States", "North America", "America/New_York"),
            "Chicago": self._location_details("Chicago", 41.8781, -87.6298, True, "US", "United States", "North America", "America/Chicago"),
            "Washington": self._location_details("Washington", 38.9072, -77.0369, True, "US", "United States", "North America", "America/New_York"),
            "Los Angeles": self._location_details("Los Angeles", 34.0522, -118.2437, True, "US", "United States", "North America", "America/Los_Angeles"),
            "Miami": self._location_details("Miami", 25.7617, -80.1918, True, "US", "United States", "North America", "America/New_York"),
            "Phoenix": self._location_details("Phoenix", 33.4484, -112.0740, True, "US", "United States", "North America", "America/Phoenix"),
            "Dallas": self._location_details("Dallas", 32.7767, -96.7970, True, "US", "United States", "North America", "America/Chicago"),
            "Houston": self._location_details("Houston", 29.7604, -95.3698, True, "US", "United States", "North America", "America/Chicago"),
            "Denver": self._location_details("Denver", 39.7392, -104.9903, True, "US", "United States", "North America", "America/Denver"),
            "Atlanta": self._location_details("Atlanta", 33.7490, -84.3880, True, "US", "United States", "North America", "America/New_York"),
            "Seattle": self._location_details("Seattle", 47.6062, -122.3321, True, "US", "United States", "North America", "America/Los_Angeles"),
            "San Francisco": self._location_details("San Francisco", 37.7749, -122.4194, True, "US", "United States", "North America", "America/Los_Angeles"),
            "Boston": self._location_details("Boston", 42.3601, -71.0589, True, "US", "United States", "North America", "America/New_York"),

            # International Markets (is_us=False -> uses Open-Meteo multi-model)
            "London": self._location_details("London", 51.5074, -0.1278, False, "GB", "United Kingdom", "Europe", "Europe/London"),
            "Paris": self._location_details("Paris", 48.8566, 2.3522, False, "FR", "France", "Europe", "Europe/Paris"),
            "Tokyo": self._location_details("Tokyo", 35.6762, 139.6503, False, "JP", "Japan", "Asia", "Asia/Tokyo"),
            "Amsterdam": self._location_details("Amsterdam", 52.3676, 4.9041, False, "NL", "Netherlands", "Europe", "Europe/Amsterdam"),
            "Hong Kong": self._location_details("Hong Kong", 22.3193, 114.1694, False, "HK", "Hong Kong", "Asia", "Asia/Hong_Kong"),
            "Singapore": self._location_details("Singapore", 1.3521, 103.8198, False, "SG", "Singapore", "Asia", "Asia/Singapore"),
            "Sydney": self._location_details("Sydney", -33.8688, 151.2093, False, "AU", "Australia", "Oceania", "Australia/Sydney"),
            "Berlin": self._location_details("Berlin", 52.5200, 13.4050, False, "DE", "Germany", "Europe", "Europe/Berlin"),
            "Toronto": self._location_details("Toronto", 43.6532, -79.3832, False, "CA", "Canada", "North America", "America/Toronto"),
            "Dubai": self._location_details("Dubai", 25.2048, 55.2708, False, "AE", "United Arab Emirates", "Asia", "Asia/Dubai"),
            "Mumbai": self._location_details("Mumbai", 19.0760, 72.8777, False, "IN", "India", "Asia", "Asia/Kolkata"),
            "Delhi": self._location_details("Delhi", 28.6139, 77.2090, False, "IN", "India", "Asia", "Asia/Kolkata"),
            "Beijing": self._location_details("Beijing", 39.9042, 116.4074, False, "CN", "China", "Asia", "Asia/Shanghai"),
            "Seoul": self._location_details("Seoul", 37.5665, 126.9780, False, "KR", "South Korea", "Asia", "Asia/Seoul"),
            "Madrid": self._location_details("Madrid", 40.4168, -3.7038, False, "ES", "Spain", "Europe", "Europe/Madrid"),
            "Rome": self._location_details("Rome", 41.9028, 12.4964, False, "IT", "Italy", "Europe", "Europe/Rome"),
            "Taipei": self._location_details("Taipei", 25.0330, 121.5654, False, "TW", "Taiwan", "Asia", "Asia/Taipei"),
            "Shenzhen": self._location_details("Shenzhen", 22.5431, 114.0579, False, "CN", "China", "Asia", "Asia/Shanghai"),
            "Wuhan": self._location_details("Wuhan", 30.5928, 114.3055, False, "CN", "China", "Asia", "Asia/Shanghai"),
            "Sao Paulo": self._location_details("Sao Paulo", -23.5505, -46.6333, False, "BR", "Brazil", "South America", "America/Sao_Paulo"),
            "Istanbul": self._location_details("Istanbul", 41.0082, 28.9784, False, "TR", "Turkey", "Europe", "Europe/Istanbul"),
            "Shanghai": self._location_details("Shanghai", 31.2304, 121.4737, False, "CN", "China", "Asia", "Asia/Shanghai"),
            "Chongqing": self._location_details("Chongqing", 29.4316, 106.9123, False, "CN", "China", "Asia", "Asia/Shanghai"),
            "Helsinki": self._location_details("Helsinki", 60.1699, 24.9384, False, "FI", "Finland", "Europe", "Europe/Helsinki"),
            "Moscow": self._location_details("Moscow", 55.7558, 37.6173, False, "RU", "Russia", "Europe", "Europe/Moscow"),
            "Bangkok": self._location_details("Bangkok", 13.7563, 100.5018, False, "TH", "Thailand", "Asia", "Asia/Bangkok"),
            "Jakarta": self._location_details("Jakarta", -6.2088, 106.8456, False, "ID", "Indonesia", "Asia", "Asia/Jakarta"),
            "Mexico City": self._location_details("Mexico City", 19.4326, -99.1332, False, "MX", "Mexico", "North America", "America/Mexico_City"),
            "Cairo": self._location_details("Cairo", 30.0444, 31.2357, False, "EG", "Egypt", "Africa", "Africa/Cairo"),
            "Lagos": self._location_details("Lagos", 6.5244, 3.3792, False, "NG", "Nigeria", "Africa", "Africa/Lagos"),
            "Buenos Aires": self._location_details("Buenos Aires", -34.6037, -58.3816, False, "AR", "Argentina", "South America", "America/Argentina/Buenos_Aires"),
            "Lima": self._location_details("Lima", -12.0464, -77.0428, False, "PE", "Peru", "South America", "America/Lima"),
            "Bogota": self._location_details("Bogota", 4.7110, -74.0721, False, "CO", "Colombia", "South America", "America/Bogota"),
            "Osaka": self._location_details("Osaka", 34.6937, 135.5023, False, "JP", "Japan", "Asia", "Asia/Tokyo"),
            "Riyadh": self._location_details("Riyadh", 24.7136, 46.6753, False, "SA", "Saudi Arabia", "Asia", "Asia/Riyadh"),
            "Nairobi": self._location_details("Nairobi", -1.2921, 36.8219, False, "KE", "Kenya", "Africa", "Africa/Nairobi"),
            "Johannesburg": self._location_details("Johannesburg", -26.2041, 28.0473, False, "ZA", "South Africa", "Africa", "Africa/Johannesburg"),
            "Melbourne": self._location_details("Melbourne", -37.8136, 144.9631, False, "AU", "Australia", "Oceania", "Australia/Melbourne"),
            "Auckland": self._location_details("Auckland", -36.8485, 174.7633, False, "NZ", "New Zealand", "Oceania", "Pacific/Auckland"),
            "Warsaw": self._location_details("Warsaw", 52.2297, 21.0122, False, "PL", "Poland", "Europe", "Europe/Warsaw"),
            "Lisbon": self._location_details("Lisbon", 38.7223, -9.1393, False, "PT", "Portugal", "Europe", "Europe/Lisbon"),
            "Athens": self._location_details("Athens", 37.9838, 23.7275, False, "GR", "Greece", "Europe", "Europe/Athens"),
            "Prague": self._location_details("Prague", 50.0755, 14.4378, False, "CZ", "Czech Republic", "Europe", "Europe/Prague"),
            "Vienna": self._location_details("Vienna", 48.2082, 16.3738, False, "AT", "Austria", "Europe", "Europe/Vienna"),
            "Stockholm": self._location_details("Stockholm", 59.3293, 18.0686, False, "SE", "Sweden", "Europe", "Europe/Stockholm"),
            "Oslo": self._location_details("Oslo", 59.9139, 10.7522, False, "NO", "Norway", "Europe", "Europe/Oslo"),
            "Copenhagen": self._location_details("Copenhagen", 55.6761, 12.5683, False, "DK", "Denmark", "Europe", "Europe/Copenhagen"),
            "Zurich": self._location_details("Zurich", 47.3769, 8.5417, False, "CH", "Switzerland", "Europe", "Europe/Zurich"),
            "Munich": self._location_details("Munich", 48.1351, 11.5820, False, "DE", "Germany", "Europe", "Europe/Berlin"),
        }

        blocked_locations = {
            "Cairo",
            "Lagos",
            "Nairobi",
            "Johannesburg",
        }

        # Check hardcoded locations first
        for loc, coords in locations.items():
            if loc.lower() in market_description.lower():
                if loc in blocked_locations:
                    print(f"[MARKETS]   [SKIP] Blocked African city market: '{loc}'")
                    return None
                return coords

        # --- Step 2: Dynamic geocoding fallback ---
        return self._geocode_from_question(market_description)

    def _geocode_from_question(self, question):
        """
        Attempt to extract a city/place name from the question and geocode it
        using Open-Meteo's free geocoding API (no key required).

        Returns rich location metadata or None if nothing found.
        """
        # Try to extract a capitalized place name from the question
        # Pattern: look for capitalized words that could be city names
        # Common patterns in Polymarket weather questions:
        #   "Will the highest temperature in [City] exceed 80F?"
        #   "Will [City] temperature be above 75 degrees?"
        city_patterns = [
            r'(?:in|for|at)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)',  # "in New York"
            r'(?:Will|will)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\s+(?:temperature|temp|high|low|weather)',
            r'([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\s+(?:temperature|temp|high|low|forecast|weather)',
        ]

        candidates = []
        for pattern in city_patterns:
            matches = re.findall(pattern, question)
            candidates.extend(matches)

        # Filter out common non-city words
        skip_words = {
            "Will", "The", "What", "How", "This", "That", "Yes", "No",
            "Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
            "Saturday", "Sunday", "January", "February", "March",
            "April", "May", "June", "July", "August", "September",
            "October", "November", "December", "Today", "Tomorrow",
        }

        for candidate in candidates:
            if candidate in skip_words:
                continue
            if len(candidate) < 3:
                continue

            # Check cache first
            if candidate in self.geocode_cache:
                cached = self.geocode_cache[candidate]
                if cached is not None:
                    print(f"[MARKETS]   [GEOCODE] Cache hit: '{candidate}' -> ({cached['lat']}, {cached['lon']})")
                    return cached
                continue

            # Query Open-Meteo geocoding API
            result = self._geocode_city(candidate)
            if result:
                self.geocode_cache[candidate] = result
                return result
            else:
                self.geocode_cache[candidate] = None  # Cache misses too

        return None

    def _geocode_city(self, city_name):
        """
        Geocode a city name using Open-Meteo's free geocoding API.
        Returns rich location metadata or None.
        """
        try:
            url = "https://geocoding-api.open-meteo.com/v1/search"
            params = {
                "name": city_name,
                "count": 1,
                "language": "en",
                "format": "json"
            }
            res = requests.get(url, params=params, timeout=10)
            res.raise_for_status()
            data = res.json()

            results = data.get("results", [])
            if not results:
                print(f"[MARKETS]   [GEOCODE] No results for '{city_name}'")
                return None

            top = results[0]
            lat = top["latitude"]
            lon = top["longitude"]
            country = top.get("country_code", "").upper()
            is_us = (country == "US")
            if country in self.blocked_country_codes:
                print(f"[MARKETS]   [SKIP] Blocked African city market: '{city_name}' in country_code={country}")
                return None

            print(f"[MARKETS]   [GEOCODE] Resolved '{city_name}' -> ({lat}, {lon}), "
                  f"country={top.get('country', '?')}, is_us={is_us}")
            return self._location_details(
                top.get("name") or city_name,
                lat,
                lon,
                is_us,
                country,
                top.get("country", ""),
                self._country_to_continent(country),
                top.get("timezone"),
            )

        except Exception as e:
            print(f"[MARKETS]   [GEOCODE] Failed for '{city_name}': {e}")
            return None

    def _location_details(self, city, lat, lon, is_us, country_code, country, continent, timezone_name):
        return {
            "city": city,
            "lat": lat,
            "lon": lon,
            "is_us": is_us,
            "country_code": country_code,
            "country": country,
            "continent": continent,
            "timezone": timezone_name or "UTC",
        }

    def _country_to_continent(self, country_code):
        code = (country_code or "").upper()
        continent_map = {
            "US": "North America",
            "CA": "North America",
            "MX": "North America",
            "AR": "South America",
            "BR": "South America",
            "CO": "South America",
            "PE": "South America",
            "GB": "Europe",
            "FR": "Europe",
            "NL": "Europe",
            "DE": "Europe",
            "ES": "Europe",
            "IT": "Europe",
            "FI": "Europe",
            "RU": "Europe",
            "PL": "Europe",
            "PT": "Europe",
            "GR": "Europe",
            "CZ": "Europe",
            "AT": "Europe",
            "SE": "Europe",
            "NO": "Europe",
            "DK": "Europe",
            "CH": "Europe",
            "TR": "Europe",
            "AE": "Asia",
            "IN": "Asia",
            "CN": "Asia",
            "KR": "Asia",
            "TW": "Asia",
            "HK": "Asia",
            "SG": "Asia",
            "TH": "Asia",
            "ID": "Asia",
            "JP": "Asia",
            "SA": "Asia",
            "AU": "Oceania",
            "NZ": "Oceania",
            "EG": "Africa",
            "NG": "Africa",
            "KE": "Africa",
            "ZA": "Africa",
        }
        return continent_map.get(code, "Unknown")


if __name__ == "__main__":
    client = MarketClient()
    markets = client.get_weather_markets()
    print(f"\n=== Found {len(markets)} weather markets ===")
    for m in markets:
        print(f"  - {m.get('question', '?')}")

    # Test geocoding fallback
    print("\n=== Testing geocoding fallback ===")
    test_questions = [
        "Will the highest temperature in Taipei exceed 35C?",
        "Will Helsinki temperature be above 20 degrees?",
        "Will the high in Reykjavik be over 15C tomorrow?",
        "What will the temperature in Anchorage be?",
    ]
    for q in test_questions:
        result = client.parse_market_location(q)
        print(f"  '{q[:50]}...' -> {result}")
