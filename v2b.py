from typing import Union

import requests


class v2b_heard:
    def __init__(self, auth_data=None):
        self.heard = {
            "Host": "cloud.5679856.xyz",
            "Connection": "keep-alive",
            "Pragma": "no-cache",
            "Cache-Control": "no-cache",
            "sec-ch-ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
            "sec-ch-ua-mobile": "?0",
            "authorization": f"{auth_data}",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/120.0.0.0 Safari/537.36",
            "sec-ch-ua-platform": '"Windows"',
            "Accept": "*/*",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
            "Accept-Encoding": 'utf-8',
            "Accept-Language": "zh-CN,zh;q=0.9",
        }

    # def upd_auth_data(self, new_auth_data):
    # self.__init__(new_auth_data)


class v2b_api:
    def __init__(self, host=None, admin_path=None, nodes_type=None):
        # self.type = node_type
        self.api = {
            "nodes_save": f"https://{host}/api/v1/{admin_path}/server/{nodes_type}/save",
            "nodes_update": f"https://{host}/api/v1/{admin_path}/server/{nodes_type}/update",
            "nodes_drop": f"https://{host}/api/v1/{admin_path}/server/{nodes_type}/drop",
            "login_api": f"https://{host}/api/v1/passport/auth/login",
            "get_override": f"https://{host}/api/v1/{admin_path}/stat/getOverride",
            "get_nodes": f"https://{host}/api/v1/{admin_path}/server/manage/getNodes"
        }


class v2b_client:
    def __init__(self, host=None, admin_path=None, email=None, password=None):
        self.account = {
            "email": email,
            "password": password
        }
        self.host = host
        self.admin_path = admin_path
        self.heard = v2b_heard()

    def login(self) -> str:
        sb = v2b_api(self.host, self.admin_path)
        login_api = sb.api['login_api']
        account_data = requests.post(login_api, self.account)
        if account_data.status_code == 200:
            self.heard = v2b_heard(account_data.json()['data']['auth_data'])
            # self.heard.upd_auth_data(account_data.json()['data']['auth_data'])
            return 'login successes'
        else:
            return 'login fail'

    def get_override(self) -> list:
        override = list()
        sb = v2b_api(self.host, self.admin_path)
        get_override = sb.api['get_override']
        override_data = requests.get(headers=self.heard.heard, url=get_override)
        if override_data.status_code == 200:
            # print(override_data.json())
            override.append(f"本月收入：{int(override_data.json()['data']['month_income']) / 100} "
                            f"今日收入：{int(override_data.json()['data']['day_income']) / 100} " 
                            f"本月新增用户：{override_data.json()['data']['month_register_total']} "
                            f"本月佣金支出：{int(override_data.json()['data']['commission_month_payout']) / 100}")

        return override

    def get_nodes(self) -> list:
        node_list = list()
        sb = v2b_api(self.host, self.admin_path)
        get_nodes = sb.api['get_nodes']
        nodes_data = requests.get(headers=self.heard.heard, url=get_nodes)
        if nodes_data.status_code == 200:
            print(nodes_data.json()['data'])
            for d in nodes_data.json()['data']:
                if d['show'] == 0:
                    tag = '离线'
                else:
                    tag = '在线'
                node_list.append(f"节点：{d['name']}  状态：{tag}  在线人数：{d['online']}")

            return node_list


data = {
    "email": '9573@qq.com',
    "password": 'kaijichina123'

}
# a = v2b_client("cloud.5679856.xyz", "happychina", data['email'], data['password'])
# print(a.login())
# a.get_nodes()
# print(a.get_override())
# api1 = "https://cloud.5679856.xyz/api/v1/happychina/server/manage/getNodes"
# res1 = requests.get(api1, headers=heard1)
# for data1 in res1.json()['data']:
#    print(data1)
