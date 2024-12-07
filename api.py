import requests


# import json

def status(response):
    if response.status_code == 200:
        data = response.json()
        return data
    else:
        return response.status_code


class dns_api:

    def __init__(self, api_token, zone_id):
        self.api_token = api_token
        self.zone_id = zone_id
        self.headers = {
            f"Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json"
        }

    def check_id(self, name):
        for dns_data in self.inspect_api()['result']:
            if dns_data['name'] == name:
                return dns_data['id']

    def inspect_api(self):
        url = f"https://api.cloudflare.com/client/v4/zones/{self.zone_id}/dns_records"
        response = requests.get(url, headers=self.headers)
        return status(response)

    def add_api(self, records_data):

        url = f"https://api.cloudflare.com/client/v4/zones/{self.zone_id}/dns_records"
        response = requests.post(url, headers=self.headers, json=records_data)
        return status(response)

    def update_api(self, record_id, record_data):

        url = f"https://api.cloudflare.com/client/v4/zones/{self.zone_id}/dns_records/{record_id}"
        response = requests.put(url, headers=self.headers, json=record_data)
        return status(response)

    def delete_api(self, record_id):

        url = f"https://api.cloudflare.com/client/v4/zones/{self.zone_id}/dns_records/{record_id}"
        response = requests.delete(url, headers=self.headers)
        return status(response)
