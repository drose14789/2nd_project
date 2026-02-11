"""
Services Module
"""
from .geoapify import autocomplete_place, autocomplete_city, places_near, hotels_near
from .amadeus import search_locations, hotels_by_city, hotel_offers
from .images import unsplash_first_image, get_city_image, get_place_image, get_hotel_image
from .ors import directions, summarize_duration_distance, get_route_summary
from .tips import get_city_tips, get_city_foods, generate_tip

# ai_explain (LLM+RAG)
from .ai_explain import generate_day_explanation, build_day_context
from .rag_engine import search as rag_search
