import asyncio

from robosen_py import K1


async def main() -> None:
    k1 = K1()
    await k1.on()
    await k1.handshake()
    await k1.volume(100)
    await k1.auto_stand(False)
    await k1.move_forward(1500)
    await k1.left_punch()
    await k1.head_left()
    await k1.left_hand('+40%', 30)
    await k1.audio('AppSysMS/101')
    await k1.wait(3000)
    await k1.end()


if __name__ == "__main__":
    asyncio.run(main())
