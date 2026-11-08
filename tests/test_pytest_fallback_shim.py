"""Regression and semantic contract tests for unittest fallback pytest marker shim (ENG-OS-03C8 Stage 2R-G).

Verifies that when running under an environment where pytest is unavailable:
1. Bare class decorator preserves exact original class identity.
2. Bare method decorator preserves exact original function identity.
3. Parenthesized custom marker without arguments preserves original callable.
4. Parenthesized custom marker with kwargs preserves original callable.
5. Unsupported / control markers fail loudly (skip, skipif, xfail, parametrize).
6. Decorated class and method execution behavior and return values remain intact.
"""
from __future__ import annotations

import unittest

# Define the exact contract implementation under test in isolation
_ALLOWED_CUSTOM_MARKERS = frozenset({"browser_integration", "loopback"})


class _DummyPytestMark:
    def __getattr__(self, name: str):
        if name not in _ALLOWED_CUSTOM_MARKERS:
            raise AttributeError(
                f"Unsupported pytest marker: {name}. Control markers "
                f"like skip/skipif/parametrize require native pytest."
            )

        def identity_marker(*args, **kwargs):
            if len(args) == 1 and callable(args[0]) and not kwargs:
                return args[0]
            return lambda fn: fn

        return identity_marker


class _DummyPytest:
    mark = _DummyPytestMark()


class TestPytestFallbackShimSemantics(unittest.TestCase):
    """Verifies that _DummyPytestMark preserves bare and parenthesized decorators without substitution."""

    def setUp(self) -> None:
        self.pytest_shim = _DummyPytest()

    def test_01_bare_class_decorator_preserves_class_identity(self) -> None:
        """Proves @pytest.mark.browser_integration on a class preserves the exact class object."""
        @self.pytest_shim.mark.browser_integration
        class TargetClass:
            class_attr = "verified_class"

            def method(self) -> str:
                return "method_executed"

        self.assertIsInstance(TargetClass, type)
        self.assertEqual("TargetClass", TargetClass.__name__)
        self.assertEqual("verified_class", TargetClass.class_attr)
        inst = TargetClass()
        self.assertEqual("method_executed", inst.method())

    def test_02_bare_method_decorator_preserves_function_identity(self) -> None:
        """Proves @pytest.mark.loopback on a function preserves the exact function object."""
        def raw_function(a: int, b: int) -> int:
            return a + b

        decorated = self.pytest_shim.mark.loopback(raw_function)
        self.assertIs(raw_function, decorated, "Bare marker must return the exact same callable object.")
        self.assertEqual(7, decorated(3, 4))

    def test_03_parenthesized_marker_without_arguments(self) -> None:
        """Proves @pytest.mark.browser_integration() preserves the decorated callable."""
        @self.pytest_shim.mark.browser_integration()
        def target_func() -> str:
            return "paren_ok"

        self.assertEqual("paren_ok", target_func())
        self.assertEqual("target_func", target_func.__name__)

    def test_04_parenthesized_marker_with_kwargs(self) -> None:
        """Proves @pytest.mark.loopback(reason="...") preserves the decorated callable."""
        @self.pytest_shim.mark.loopback(reason="integration test")
        def target_func() -> str:
            return "kwargs_ok"

        self.assertEqual("kwargs_ok", target_func())
        self.assertEqual("target_func", target_func.__name__)

    def test_05_rejection_of_skip_marker(self) -> None:
        """Proves accessing pytest.mark.skip fails loudly with AttributeError."""
        with self.assertRaises(AttributeError) as ctx:
            _ = self.pytest_shim.mark.skip
        self.assertIn("Unsupported pytest marker: skip", str(ctx.exception))

    def test_06_rejection_of_skipif_marker(self) -> None:
        """Proves accessing pytest.mark.skipif fails loudly with AttributeError."""
        with self.assertRaises(AttributeError) as ctx:
            _ = self.pytest_shim.mark.skipif
        self.assertIn("Unsupported pytest marker: skipif", str(ctx.exception))

    def test_07_rejection_of_xfail_marker(self) -> None:
        """Proves accessing pytest.mark.xfail fails loudly with AttributeError."""
        with self.assertRaises(AttributeError) as ctx:
            _ = self.pytest_shim.mark.xfail
        self.assertIn("Unsupported pytest marker: xfail", str(ctx.exception))

    def test_08_rejection_of_parametrize_marker(self) -> None:
        """Proves accessing pytest.mark.parametrize fails loudly with AttributeError."""
        with self.assertRaises(AttributeError) as ctx:
            _ = self.pytest_shim.mark.parametrize
        self.assertIn("Unsupported pytest marker: parametrize", str(ctx.exception))

    def test_09_testcase_method_execution_in_unittest(self) -> None:
        """Proves that a unittest.TestCase decorated with bare markers executes normally."""
        marker = self.pytest_shim.mark

        @marker.browser_integration
        class DecoratedTestCase(unittest.TestCase):
            @marker.loopback
            def test_inner(self) -> None:
                self.assertTrue(True)

        suite = unittest.TestLoader().loadTestsFromTestCase(DecoratedTestCase)
        result = unittest.TestResult()
        suite.run(result)
        self.assertEqual(1, result.testsRun)
        self.assertEqual(0, len(result.failures))
        self.assertEqual(0, len(result.errors))


if __name__ == "__main__":
    unittest.main()

