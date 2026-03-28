import asyncio

from robosen_py import K1


async def main() -> None:
    robot = K1()
    await robot.on()
    try:
        while True:
          raw = input(f"{robot.name}> ").strip()
          if not raw:
              continue
          if raw.lower() in {"exit", "quit"}:
              break
          await robot.stop()
          await robot.command(raw, limit=True)
    finally:
        await robot.end()


if __name__ == "__main__":
    asyncio.run(main())
