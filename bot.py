from dns import Dns
from v2b import v2b_heard, v2b_client
from v2b import v2b_api
import time
from ssh import ss_config, hysteria2_config, server

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ConversationHandler, ContextTypes


class bot:
    def __init__(self):
        self.client = v2b_client()
        self.config = ss_config()
        self.user_id = [1066223787, 6498786357]

    def check_user_id(self, update: Update) -> int:
        user_id = update.message.from_user.id
        if user_id in self.user_id:
            return 1
        else:
            return 0

    async def check_login(self, update: Update, _v2b_heard: v2b_heard) -> int:
        if self.check_user_id(update):
            if _v2b_heard.heard['authorization'] == 'None':
                await update.message.reply_text('未登录，请登录')
                return 0
            else:
                return 1

    async def login(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self.check_user_id(update):
            self.client: v2b_client = v2b_client('cloud.5679856.xyz', 'happychina', context.args[0], context.args[1])
            await update.message.reply_text(self.client.login())

    async def get_nodes(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self.check_user_id(update):
            if await self.check_login(update, _v2b_heard=self.client.heard):
                for data in self.client.get_nodes():
                    time.sleep(1)
                    await update.message.reply_text(data)

    async def exit_login(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self.check_user_id(update):
            if await self.check_login(update, _v2b_heard=self.client.heard):
                await update.message.reply_text('Log out successes')
                self.client.heard.heard['authorization'] = 'None'

    async def get_override(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self.check_user_id(update):
            if await self.check_login(update, _v2b_heard=self.client.heard):
                for data in self.client.get_override():
                    await update.message.reply_text(data)

    async def node_install(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self.check_user_id(update):
            core_dict = dict(xray='1', sinbox='2', hysteria2='3')
            node_type_dict = dict(Shadowsocks='1', Vless='2', Vmess='3', Trojan='4')
            new_server = server(hostname=context.args[0], password=context.args[1])
            if context.args[2] == 'Shadowsocks':
                self.config = ss_config(node_id=context.args[3], node_type=node_type_dict[context.args[2]],
                                        core=core_dict[context.args[4]])
            elif context.args[2] == 'hysteria2':
                self.config = hysteria2_config(core=core_dict[context.args[2]], node_id=context.args[3],
                                               node_host=context.args[4])
            if new_server.connect() == 'successes':
                await update.message.reply_text('连接成功，准备安装')
                if new_server.v2bx_install() in ('successes', 'already'):
                    await update.message.reply_text('安装成功，准备配置节点')
                    await update.message.reply_text(new_server.node_start(app_config=self.config))
                else:
                    await update.message.reply_text('安装失败')
            else:
                await update.message.reply_text('连接失败')

    def bot_start(self):
        app = ApplicationBuilder().token('1778608880:AAHzEUa2ZCXGRhfYc0IxqtefmwjTppq72A0').build()
        app.add_handler(CommandHandler("start", self.login))
        app.add_handler(CommandHandler('node', self.get_nodes))
        app.add_handler(CommandHandler('exit_login', self.exit_login))
        app.add_handler(CommandHandler('override', self.get_override))
        app.add_handler(CommandHandler('install', self.node_install))
        app.run_polling()


a = bot()
a.bot_start()
