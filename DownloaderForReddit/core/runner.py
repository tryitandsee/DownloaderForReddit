def verify_run(method):
    """
    Decorator method that verifies that the containing class's continue_run variable is set to True before calling the
    supplied method.
    :param method: The method that should be called if the run condition is met.
    """

    def check(instance, *args, **kwargs):
        if instance.continue_run:
            return method(instance, *args, **kwargs)
        return None

    return check


class Runner:
    def __init__(self, stop_run):
        self.stop_run = stop_run

    @property
    def continue_run(self):
        return not self.stop_run.is_set()
