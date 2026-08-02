"""继承 + 多态全笼罩 fixture。"""


class Animal:
    def sound(self):
        return "..."


class Dog(Animal):
    def sound(self):
        return "Woof"


class Cat(Animal):
    def sound(self):
        return "Meow"


def make_sound(a):
    return a.sound()


def static_demo():
    return Dog.sound.__qualname__
