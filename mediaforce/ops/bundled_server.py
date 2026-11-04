import sys


def main() -> int:
    import truststore

    truststore.inject_into_ssl()
    if "service" in sys.argv[1:]:
        from mediaforce.cli import main as cli_main

        return cli_main()
    from mediaforce.web.app import main as web_main

    web_main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
