"""
Real Estate Search Tools for Azure AI Voice Live Service (with Vector Search)
==============================================================================
Provides property search functionality using Azure AI Search with hybrid
(keyword + vector) search for better semantic matching.
"""

import os
import json
import requests
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Azure AI Search Configuration
SEARCH_ENDPOINT = os.getenv("AZURE_SEARCH_ENDPOINT")
SEARCH_API_KEY = os.getenv("AZURE_SEARCH_API_KEY")
INDEX_NAME = os.getenv("AZURE_SEARCH_INDEX_NAME", "ezdan-properties")

# Azure OpenAI Configuration
OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT")
OPENAI_API_KEY = os.getenv("AZURE_OPENAI_API_KEY")
EMBEDDING_DEPLOYMENT = os.getenv("AZURE_OPENAI_EMBEDDING_DEPLOYMENT", "text-embedding-3-large")

# API Versions
SEARCH_API_VERSION = "2024-07-01"
OPENAI_API_VERSION = "2024-02-01"
EMBEDDING_DIMENSIONS = 3072

# Tool definitions for Azure Voice Live API
REAL_ESTATE_TOOLS = [
    {
        "type": "function",
        "name": "search_properties",
        "description": "Search for real estate properties based on various criteria like location, property type, price range, and number of bedrooms. Use this when the customer asks about available properties, apartments, villas, commercial spaces, or any real estate listings.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Free text search query. Can include location names, property features, or any keywords. Examples: 'Al Wakra apartment', 'commercial space', '3 bedroom villa', 'family home'"
                },
                "property_type": {
                    "type": "string",
                    "enum": ["Apartment", "Villa", "Commercial", ""],
                    "description": "Type of property to filter by. Leave empty to include all types."
                },
                "location": {
                    "type": "string",
                    "description": "Location/area to filter by. Examples: 'Al Wakra', 'Ezdan Oasis', 'Doha'"
                },
                "min_price": {
                    "type": "integer",
                    "description": "Minimum monthly rent in QAR"
                },
                "max_price": {
                    "type": "integer",
                    "description": "Maximum monthly rent in QAR"
                },
                "bedrooms": {
                    "type": "integer",
                    "description": "Number of bedrooms required. Use 0 for studios or commercial properties."
                }
            },
            "required": ["query"]
        }
    },
    {
        "type": "function",
        "name": "get_property_details",
        "description": "Get detailed information about a specific property by its reference number. Use this when the customer asks for more details about a particular property they're interested in.",
        "parameters": {
            "type": "object",
            "properties": {
                "reference_number": {
                    "type": "string",
                    "description": "The property reference number (e.g., 'JG-SHOP-A10', 'EOA2-3BHK-FF-A')"
                }
            },
            "required": ["reference_number"]
        }
    }
]


def _get_search_headers():
    """Get headers for Azure AI Search API requests."""
    return {
        "Content-Type": "application/json",
        "api-key": SEARCH_API_KEY
    }


def _get_openai_headers():
    """Get headers for Azure OpenAI API requests."""
    return {
        "Content-Type": "application/json",
        "api-key": OPENAI_API_KEY
    }


def _generate_embedding(text: str) -> list:
    """
    Generate embedding vector for text using Azure OpenAI.
    
    Args:
        text: The text to embed
        
    Returns:
        List of floats representing the embedding vector, or None on failure
    """
    if not OPENAI_ENDPOINT or not OPENAI_API_KEY:
        return None
    
    try:
        url = f"{OPENAI_ENDPOINT}/openai/deployments/{EMBEDDING_DEPLOYMENT}/embeddings?api-version={OPENAI_API_VERSION}"
        
        payload = {
            "input": text,
            "dimensions": EMBEDDING_DIMENSIONS
        }
        
        response = requests.post(url, headers=_get_openai_headers(), json=payload, timeout=10)
        
        if response.status_code == 200:
            result = response.json()
            return result["data"][0]["embedding"]
        else:
            return None
            
    except Exception:
        return None


async def search_properties(
    query: str,
    property_type: str = "",
    location: str = "",
    min_price: int = None,
    max_price: int = None,
    bedrooms: int = None
) -> str:
    """
    Hybrid search for properties using keyword + vector search.
    
    Returns JSON string with search results.
    """
    if not SEARCH_ENDPOINT or not SEARCH_API_KEY:
        return json.dumps({"error": "Search service not configured"})
    
    url = f"{SEARCH_ENDPOINT}/indexes/{INDEX_NAME}/docs/search?api-version={SEARCH_API_VERSION}"
    
    # Build filter expressions
    filters = []
    
    if property_type:
        filters.append(f"property_type eq '{property_type}'")
    
    if location:
        filters.append(f"search.ismatch('{location}', 'location')")
    
    if min_price is not None:
        filters.append(f"price ge {min_price}")
    
    if max_price is not None:
        filters.append(f"price le {max_price}")
    
    if bedrooms is not None:
        filters.append(f"bedrooms eq {bedrooms}")
    
    # Build search payload - start with keyword search
    payload = {
        "search": query,
        "top": 10,
        "select": "id,reference_number,title,property_type,location,price,bedrooms,bathrooms,url",
        "count": True,
        "queryType": "simple"
    }
    
    # Add vector search if embeddings are available
    query_embedding = _generate_embedding(query)
    if query_embedding:
        payload["vectorQueries"] = [
            {
                "kind": "vector",
                "vector": query_embedding,
                "fields": "content_vector",
                "k": 10
            }
        ]
    
    if filters:
        payload["filter"] = " and ".join(filters)
    
    try:
        response = requests.post(url, headers=_get_search_headers(), json=payload, timeout=15)
        
        if response.status_code == 200:
            result = response.json()
            properties = result.get("value", [])
            total_count = result.get("@odata.count", 0)
            
            # Format response for voice agent
            if not properties:
                return json.dumps({
                    "found": 0,
                    "message": "No properties found matching your criteria."
                })
            
            formatted_properties = []
            for prop in properties:
                formatted_properties.append({
                    "reference": prop.get("reference_number"),
                    "title": prop.get("title"),
                    "type": prop.get("property_type"),
                    "location": prop.get("location"),
                    "price_qar": prop.get("price"),
                    "bedrooms": prop.get("bedrooms"),
                    "bathrooms": prop.get("bathrooms")
                })
            
            return json.dumps({
                "found": total_count,
                "showing": len(formatted_properties),
                "properties": formatted_properties
            })
        else:
            return json.dumps({"error": f"Search failed: {response.status_code}"})
            
    except Exception as e:
        return json.dumps({"error": f"Search error: {str(e)}"})


async def get_property_details(reference_number: str) -> str:
    """
    Get details of a specific property by reference number using hybrid search.
    
    Returns JSON string with property details.
    """
    if not SEARCH_ENDPOINT or not SEARCH_API_KEY:
        return json.dumps({"error": "Search service not configured"})
    
    url = f"{SEARCH_ENDPOINT}/indexes/{INDEX_NAME}/docs/search?api-version={SEARCH_API_VERSION}"
    
    # Build payload - use exact match on reference number
    payload = {
        "search": reference_number,
        "searchFields": "reference_number",
        "top": 1,
        "select": "id,reference_number,title,property_type,location,price,bedrooms,bathrooms,url,image_url"
    }
    
    # Also add vector search for semantic matching
    query_embedding = _generate_embedding(f"property reference {reference_number}")
    if query_embedding:
        payload["vectorQueries"] = [
            {
                "kind": "vector",
                "vector": query_embedding,
                "fields": "content_vector",
                "k": 1
            }
        ]
    
    try:
        response = requests.post(url, headers=_get_search_headers(), json=payload, timeout=10)
        
        if response.status_code == 200:
            result = response.json()
            properties = result.get("value", [])
            
            if not properties:
                return json.dumps({
                    "found": False,
                    "message": f"No property found with reference number {reference_number}"
                })
            
            prop = properties[0]
            return json.dumps({
                "found": True,
                "property": {
                    "reference": prop.get("reference_number"),
                    "title": prop.get("title"),
                    "type": prop.get("property_type"),
                    "location": prop.get("location"),
                    "price_qar": prop.get("price"),
                    "bedrooms": prop.get("bedrooms"),
                    "bathrooms": prop.get("bathrooms"),
                    "url": prop.get("url")
                }
            })
        else:
            return json.dumps({"error": f"Lookup failed: {response.status_code}"})
            
    except Exception as e:
        return json.dumps({"error": f"Lookup error: {str(e)}"})
