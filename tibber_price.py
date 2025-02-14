import aiohttp

# Define the endpoint and headers
url = "https://api.tibber.com/v1-beta/gql"
headers = {
    "Authorization": "TDUWuDv72DujIV4CtCmZkKYja8E2USybaNo5XqbgLLw",
    "Content-Type": "application/json",
    "User-Agent": "REST",
}

# Define the payload
payload = {
    "query": """
      {
    viewer {
      login
      name
      home(id:"1cfebe9b-dd00-4a24-80a8-9cc481ce4fbb") {
        id
        currentSubscription {
          priceInfo {
            current {total}
            today {
              total
              startsAt
            }
            tomorrow {
              total
              startsAt
            }
          }
        }
      }
    }
  }
    """
}


#@time_trigger  # run on reload
#@time_trigger("cron(*/1 * * * *)")
async def calculate_target_time_and_energy():
    async with aiohttp.ClientSession() as client:
        response = await client.post(url, headers=headers, json=payload)
        if response.status == 200:
            data = response.json()
            price_info = (
                data.get("data", {})
                .get("viewer", {})
                .get("home", {})
                .get("currentSubscription", {})
                .get("priceInfo", {})
            )
            if price_info is not None:
                price = price_info.get("current", {}).get("total")
                sensor.electricity_price = price
                sensor.electricity_price_hauptstrasse_134 = price
                sensor.electricity_price.today = price_info.get("today")
                sensor.electricity_price.tomorrow = price_info.get("tomorrow")

            else:
                log.error("Error getting tibber prices:", response.status, response.text)
                return None
