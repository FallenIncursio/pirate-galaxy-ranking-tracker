from pg_rankings.window_profile import restore_client_size

EXPECTED_WIDTH = 800
EXPECTED_HEIGHT = 600


def main() -> None:
    restored = restore_client_size(
        "PirateGalaxy Version:",
        expected_width=EXPECTED_WIDTH,
        expected_height=EXPECTED_HEIGHT,
    )
    print(
        f"RESTORE_OK client={restored.width}x{restored.height} "
        f"origin={restored.left},{restored.top} "
        f"window={restored.window_left},{restored.window_top}"
    )


if __name__ == "__main__":
    main()
