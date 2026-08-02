"""反射 fixture：getattr 硬编码全笼罩。"""


class ServiceA:
    def run(self):
        return "A"


class ServiceB:
    def run(self):
        return "B"


def call_by_name(svc):
    return getattr(svc, "run")()
