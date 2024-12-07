from api import dns_api


class Dns:

    def __init__(self, api_token, zone_id):
        # self.api_token = api_token
        # self.zone_id = zone_id
        self.api1 = dns_api(api_token, zone_id)

    def delete_record(self, name):
        record_id = self.api1.check_id(name=name)
        return self.api1.delete_api(record_id=record_id)

    def update_record(self, dns_record, name):
        record_id = self.api1.check_id(name=name)
        return self.api1.update_api(record_id=record_id,
                                    record_data=dns_record)

    def add_record(self, dns_record):
        return self.api1.add_api(records_data=dns_record)

    def search_record(self):
        datadict = {}
        Rerecord = self.api1.inspect_api()
        # return Rerecord
        if not Rerecord['result']:
            return "当前域名无解析记录"
        else:
            # print(len(Rerecord['result']))
            for rerecord_id in range(len(Rerecord['result'])):
                # print(f"域名 ：{rerecord['name']}\n类型：{rerecord['type']}\n值：{rerecord['content']}")
                datadict[rerecord_id] = {}
                for i in range(1):
                    print(Rerecord['result'][rerecord_id]['name'])
                # datadict[rerecord_id]["sd"] = Rerecord['result']['rerecord_id']['name']
                # datadict[rerecord_id]["ad"] = Rerecord['result']['rerecord_id']['type']
                # datadict[rerecord_id]["adf"] = Rerecord['result']['rerecord_id']['content']



