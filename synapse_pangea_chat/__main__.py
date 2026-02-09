def main() -> None:
    import argparse
    from importlib.metadata import PackageNotFoundError, version

    parser = argparse.ArgumentParser(description="synapse-pangea-chat module")

    parser.add_argument(
        "--version",
        "-v",
        action="store_true",
        help="Display the version",
    )

    args = parser.parse_args()

    if args.version:
        try:
            project_version = version("synapse_pangea_chat")
            print(f"Version {project_version}")
        except PackageNotFoundError:
            print("Version information not available.")


if __name__ == "__main__":
    main()
