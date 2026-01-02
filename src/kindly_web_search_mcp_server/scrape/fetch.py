import httpx

async def fetch_url(url: str) -> str:
    """
    Fetches the HTML content of a URL.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    }
    async with httpx.AsyncClient(follow_redirects=True) as client:
        try:
            response = await client.get(url, headers=headers, timeout=15.0)
            response.raise_for_status()
            return response.text
        except httpx.RequestError as e:
            return f"Error fetching URL {url}: {e}"
        except httpx.HTTPStatusError as e:
            return f"Error response {e.response.status_code} while requesting {e.request.url}."
