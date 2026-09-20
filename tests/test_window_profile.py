from pg_rankings.window_profile import corrected_outer_size


def test_corrected_outer_size_removes_measured_java_client_inset_error() -> None:
    assert corrected_outer_size(
        980,
        659,
        actual_client_width=964,
        actual_client_height=620,
        expected_client_width=954,
        expected_client_height=610,
    ) == (970, 649)


def test_corrected_outer_size_can_expand_an_undersized_client() -> None:
    assert corrected_outer_size(
        960,
        640,
        actual_client_width=944,
        actual_client_height=600,
        expected_client_width=954,
        expected_client_height=610,
    ) == (970, 650)
