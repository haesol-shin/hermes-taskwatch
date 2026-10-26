# Spec

A check that exceeds its time limit raises `TimeoutError`. Report it as a result with status `timeout` instead of letting the error abort the whole run.
