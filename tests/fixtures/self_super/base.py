"""self / super / 类方法调用 + 继承 fixture。"""


class Base:
    def greet(self):
        return "hi"

    @staticmethod
    def help():
        return "help"


class Child(Base):
    def greet(self):
        return super().greet()   # super() → Base.greet

    def call_self(self):
        return self.greet()      # self → Child.greet

    def call_static(self):
        return Base.help()       # 类名静态调用 → Base.help
