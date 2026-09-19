
import unittest
from app.main import Handler


class HealthTest(unittest.TestCase):
    def test_handler_has_health_method(self) -> None:
        self.assertTrue(callable(Handler.do_GET))


if __name__ == "__main__":
    unittest.main()
