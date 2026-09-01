# Cosine Similarity, From Scratch

The key idea is that **cosine similarity is basically a dot product that has
been adjusted so vector length doesn't matter**.

## 1. Start with dot product

Imagine two embedding vectors:

```python
A = [1, 2]
B = [2, 3]
```

Their dot product is:

$$
A \cdot B = (1 \times 2) + (2 \times 3) = 8
$$

A bigger dot product generally suggests the vectors point more similarly —
**but there's a problem:** longer vectors naturally tend to produce bigger
dot products.

So dot product is affected by both:

- the **direction** of the vectors
- their **length/magnitude**

## 2. Cosine similarity removes the length

Cosine similarity is:

$$
\text{cosine}(A,B) = \frac{A \cdot B}{|A| \times |B|}
$$

The bottom part just divides out the lengths of vectors *A* and *B*.

So cosine similarity is effectively asking:

> "Ignoring how long these vectors are, how closely do they point in the
> same direction?"

For embeddings, that's often what we care about.

## 3. What does "normalizing" an embedding mean?

Normalization means: change the vector's length to $1$, while keeping its
direction exactly the same.

For example, conceptually:

```text
Original vector
[3, 4]

length = 5
```

Normalize it by dividing everything by 5:

```text
[3/5, 4/5]
= [0.6, 0.8]
```

Now its length is exactly $1$. Nothing about its direction changed.

## 4. Dot product and cosine become the same thing

Remember:

$$
\text{cosine}(A,B) = \frac{A \cdot B}{|A||B|}
$$

If we've normalized both vectors, $|A| = 1$ and $|B| = 1$, then:

$$
\text{cosine}(A,B) = \frac{A \cdot B}{1 \times 1}
$$

Therefore:

$$
\text{cosine}(A,B) = A \cdot B
$$

That's the important bit — **on normalized vectors, cosine similarity and
dot product give exactly the same score.**

## 5. Sub/superscript examples

A few notation shortcuts worth calling out directly rather than as images,
since they're simple enough to just read as text:

- Squaring a value: $x^2$
- A subscripted vector component: $x_i$
- A chemical formula (not math, but the same sup/sub machinery): $H_2O$

## 6. Quick reference table

| Term            | Symbol      | Notes                                  |
| ---------------- | ----------- | --------------------------------------- |
| Dot product      | $A \cdot B$ | Sum of elementwise products             |
| Vector length    | $|A|$     | Also called the norm or magnitude       |
| Cosine similarity | $\text{cosine}(A,B)$ | Dot product, adjusted for length |
| Normalized vector | unit vector | Length rescaled to exactly 1            |

## 7. Nested list recap

- Dot product
  - Depends on direction
  - Depends on magnitude
- Cosine similarity
  - Depends only on direction
  - Equivalent to dot product once vectors are normalized
