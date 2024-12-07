import paramiko
import time
from typing import Union

from paramiko.channel import Channel


class ss_config:
    def __init__(self, chose='15', sure='y', save_sure='n', tls='n', w='n', appname='V2bX',
                 host='https://cloud.5679856.xyz', apikey='12345678', node_id: str = None,
                 node_type: str = None, core: str = None):
        self.value = [appname, chose, sure, host, apikey, save_sure, core, node_id, node_type, tls, w]


class hysteria2_config:
    def __init__(self, chose='15', sure='y', save_sure='n', w='n', host='https://cloud.5679856.xyz', apikey='12345678',
                 appname='V2bX', core: str = None, node_id: str = None, tls_mode='1', node_host: str = None):
        self.value = [appname, chose, sure, host, apikey, save_sure, core, node_id, tls_mode, node_host, w]


class server:
    def __init__(self, hostname, port=22, username='root', password=None):
        self.hostname = hostname
        self.port = port
        self.username = username
        self.password = password
        self.ssh_client = paramiko.SSHClient()
        self.ssh_client.load_system_host_keys()
        self.ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    def connect(self) -> str:
        try:
            self.ssh_client.connect(hostname=self.hostname, port=self.port, username=self.username,
                                    password=self.password, )
            return "successes"
        except paramiko.AuthenticationException:
            return "Authentication failed"
        except paramiko.SSHException as e:
            return f"SSH error occurred: {e}"
        except Exception as e:
            return f"An unexpected error occurred: {e}"

    def exec_command(self, command, stdin_state=None, stdin_value=None) -> str:
        if not self.ssh_client.get_transport().is_active():
            return "SSH connection is not established"
        stdin, stdout, stderr = self.ssh_client.exec_command(command)
        if stdin_state:
            stdin.write(stdin_value)
            stdin.flush()
        output = stdout.read().decode().strip()
        error = stderr.read().decode().strip()
        if error:
            return error
        else:
            return output

    def shell_command(self, channel: Channel, app_config: Union[ss_config, hysteria2_config]) -> str:
        if not self.ssh_client.get_transport().is_active():
            return "SSH connection is not established"
        for config in app_config.value:
            time.sleep(1)
            channel.send((config + '\n').encode('utf-8'))
        output = channel.recv(10240).decode('utf-8')
        return output

    def v2bx_install(self) -> str:
        stdio = self.exec_command('V2bX', 1, '17\n')
        if "command not found" in stdio:
            output = self.exec_command(
                'wget -N https://raw.githubusercontent.com/wyx2685/V2bX-script/master/install.sh '
                '&&'
                'bash install.sh', 1, 'n\n')
            stdio = self.exec_command('V2bX', 1, '17\n')
            if "command not fount" in stdio:
                return 'fail'
            else:
                return 'successes'
        else:
            return 'already'

    def node_start(self, app_config: Union[ss_config, hysteria2_config]) -> str:
        channel = self.ssh_client.invoke_shell()
        time.sleep(1)
        # channel.send('clear\n')
        output = self.shell_command(channel, app_config)
        time.sleep(1)
        # output = channel.recv(102400).decode()
        # print(output)
        self.turnoff_connect()
        # return 'successes'
        return output

    def turnoff_connect(self):
        self.ssh_client.close()


host_info = {
    'hostname': '38.60.91.119',
    'password': 'QbxC4QJe0lcr9hNEQN2d'
}

# b = ss_config(node_id='10', node_type='1', core='2')
# a = server(hostname=host_info['hostname'], password=host_info['password'])
# print(a.connect())
# my_list = ["y\n", "https://1.1.1.1\n", "123456789\n", "n\n", "1\n", "3\n", "1\n", "n\n", "n\n", "\n "]
# print(a.exec_command('V2bX generate', 1, my_list))
# a.v2bx(app_config=b)
# print(a.v2bx_install())
