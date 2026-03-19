from nonebot import on_fullmatch
from nonebot.adapters.onebot.v11 import PrivateMessageEvent

hello = on_fullmatch("ping", priority=10, block=True)


@hello.handle()
async def handle_hello(event: PrivateMessageEvent):
    await hello.finish("pong~")
