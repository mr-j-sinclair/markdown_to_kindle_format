**Question:**

What is composition in OOP?

**Answer:**

Composition means one object contains another object as an attribute,
expressing a "has-a" relationship. For example:

```python
class Car:
    def __init__(self):
        self.engine = Engine()
```

A `Car` has an `Engine`.

**Question:**

How is that different from inheritance?

**Answer:**

Inheritance describes an "is-a" relationship instead — a subclass extends
a parent class:

```python
class ElectricCar(Car):
    pass
```

Here an `ElectricCar` *is a* `Car`, whereas composition just means it
*has* one.
