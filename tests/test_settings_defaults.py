from mediaforce.config.settings import _default_app_settings


def test_default_app_settings_include_per_library_max_heights():
    settings = _default_app_settings()

    libs = {lib.id: lib for lib in settings.libraries}

    assert settings.global_max_height == 1080
    assert libs["tv"].max_height == 1080
    assert libs["movies"].max_height == 2160

