"""Quasi-Monte Carlo engines and helpers."""
import copy
import numbers
from abc import ABC, abstractmethod
import math
import warnings

import numpy as np

from scipy.optimize import basinhopping
from scipy.stats import norm
from scipy.stats._sobol import (
    initialize_v, _cscramble, _fill_p_cumulative, _draw, _fast_forward,
    _categorize, initialize_direction_numbers, _MAXDIM, _MAXBIT
)

__all__ = ['scale', 'discrepancy', 'QMCEngine', 'Sobol', 'Halton',
           'OrthogonalLatinHypercube', 'LatinHypercube', 'OptimalDesign',
           'MultinomialQMC', 'MultivariateNormalQMC']


# Based on scipy._lib._util.check_random_state
def check_random_state(seed=None):
    """Turn `seed` into a `numpy.random.Generator` instance.

    Parameters
    ----------
    seed : {None, int, `numpy.random.Generator`,
            `numpy.random.RandomState`}, optional
        If `seed` is None the `numpy.random.Generator` singleton is used.
        If `seed` is an int, a new ``Generator`` instance is used,
        seeded with `seed`.
        If `seed` is already a ``Generator`` or ``RandomState`` instance then
        that instance is used.

    Returns
    -------
    seed : {`numpy.random.Generator`, `numpy.random.RandomState`}
        Random number generator.

    """
    if seed is None or isinstance(seed, (numbers.Integral, np.integer)):
        if not hasattr(np.random, 'Generator'):
            # This can be removed once numpy 1.16 is dropped
            msg = ("NumPy 1.16 doesn't have Generator, use either "
                   "NumPy >= 1.17 or `seed=np.random.RandomState(seed)`")
            raise ValueError(msg)
        return np.random.default_rng(seed)
    elif isinstance(seed, np.random.RandomState):
        return seed
    elif isinstance(seed, np.random.Generator):
        # The two checks can be merged once numpy 1.16 is dropped
        return seed
    else:
        raise ValueError('%r cannot be used to seed a numpy.random.Generator'
                         ' instance' % seed)


def scale(sample, bounds, reverse=False):
    r"""Sample scaling from unit hypercube to bounds range.

    To convert a sample from :math:`[0, 1)` to :math:`[a, b), b>a`, the
    following transformation is used:

    .. math::

        (b - a) \cdot \text{sample} + a

    Parameters
    ----------
    sample : array_like (n, d)
        Sample to scale.
    bounds : tuple or array_like ([min, d], [max, d])
        Desired range of transformed data. If `reverse` is True, range of the
        original data to transform to the unit hypercube.
    reverse : bool, optional
        Reverse the transformation from `bounds` to the unit hypercube.
        Default is False.

    Returns
    -------
    sample : array_like (n, d)
        Scaled sample.

    Examples
    --------
    >>> from scipy.stats import qmc
    >>> bounds = [[-2, 0],
    ...           [6, 5]]
    >>> sample = [[0.5 , 0.5 ],
    ...           [0.75, 0.25]]
    >>> qmc.scale(sample, bounds)
    array([[2.  , 2.5 ],
           [4.  , 1.25]])

    """
    bounds = np.asarray(bounds)
    min_ = np.min(bounds, axis=0)
    max_ = np.max(bounds, axis=0)
    if not reverse:
        return sample * (max_ - min_) + min_
    else:
        return (sample - min_) / (max_ - min_)


def discrepancy(sample, iterative=False, method='CD'):
    """Discrepancy on a given sample.

    Parameters
    ----------
    sample : array_like (n, d)
        The sample to compute the discrepancy from.
    iterative : bool, optional
        Must be False if not using it for updating the discrepancy.
        Default is False. Refer to the notes for more details.
    method : str, optional
        Type of discrepancy, can be ['CD', 'WD', 'MD', 'star']. Refer to
        the notes for more details. Default is ``CD``.

    Returns
    -------
    discrepancy : float
        Discrepancy.

    Notes
    -----
    The discrepancy is a uniformity criterion used to assess the space filling
    of a number of samples in a hypercube.
    The discrepancy measures how the spread of the points deviates from a
    uniform distribution.
    The lower the value is, the better the coverage of the parameter space is.

    A discrepancy quantifies the distance between the continuous uniform
    distribution on a hypercube and the discrete uniform distribution on
    :math:`n` distinct sample points. Smaller values are better. For a
    collection of subsets of the hypercube, the discrepancy is the greatest
    absolute difference between the fraction of sample points in one of those
    subsets and the volume of that subset. There are different definitions of
    discrepancy corresponding to different collections of subsets. Some
    versions take a root mean square difference over subsets instead of
    a maximum.

    A measure of uniformity is reasonable if it satisfies the following
    criteria [1]_:

    1. It is invariant under permuting factors and/or runs.
    2. It is invariant under rotation of the coordinates.
    3. It can measure not only uniformity of the sample over the hypercube,
       but also the projection uniformity of the sample over non-empty
       subset of lower dimension hypercubes.
    4. There is some reasonable geometric meaning.
    5. It is easy to compute.
    6. It satisfies the Koksma-Hlawka-like inequality.
    7. It is consistent with other criteria in experimental design.

    Four methods are available:

    * ``CD``: Centered Discrepancy - subspace involves a corner of the
      hypercube
    * ``WD``: Wrap-around Discrepancy - subspace can wrap around bounds
    * ``MD``: Mixture Discrepancy - mix between CD/WD covering more criteria
    * ``star``: Star L2-discrepancy - like CD BUT variant to rotation

    Lastly, using ``iterative=True``, it is possible to compute the
    discrepancy as if we had :math:`n+1` samples. This is useful if we want
    to add a point to a sampling and check the candidate which would give the
    lowest discrepancy. Then you could just update the discrepancy with
    each candidate using `_update_discrepancy`. This method is faster than
    computing the discrepancy for a large number of candidates.

    References
    ----------
    .. [1] Fang et al. Design and modeling for computer experiments,
       Computer Science and Data Analysis Series, 2006.
    .. [2] Zhou Y.-D. et al. Mixture discrepancy for quasi-random point sets
       Journal of Complexity, 29 (3-4) , pp. 283-301, 2013.
    .. [3] T. T. Warnock. Computational investigations of low discrepancy point
       sets, Applications of Number Theory to Numerical
       Analysis, Academic Press, pp. 319-343, 1972.

    Examples
    --------
    Calculate the quality of the sample using the discrepancy:

    >>> from scipy.stats import qmc
    >>> space = np.array([[1, 3], [2, 6], [3, 2], [4, 5], [5, 1], [6, 4]])
    >>> bounds = np.array([[0.5, 0.5], [6.5, 6.5]])
    >>> space = qmc.scale(space, bounds, reverse=True)
    >>> space
    array([[0.08333333, 0.41666667],
           [0.25      , 0.91666667],
           [0.41666667, 0.25      ],
           [0.58333333, 0.75      ],
           [0.75      , 0.08333333],
           [0.91666667, 0.58333333]])
    >>> qmc.discrepancy(space)
    0.008142039609053464

    """
    sample = np.asarray(sample)

    n, d = sample.shape

    if iterative:
        n += 1

    if method == 'CD':
        abs_ = abs(sample - 0.5)
        disc1 = np.sum(np.prod(1 + 0.5 * abs_ - 0.5 * abs_ ** 2, axis=1))

        prod_arr = 1
        for i in range(d):
            s0 = sample[:, i]
            prod_arr *= (1 +
                         0.5 * abs(s0[:, None] - 0.5) + 0.5 * abs(s0 - 0.5) -
                         0.5 * abs(s0[:, None] - s0))
        disc2 = prod_arr.sum()

        return ((13.0 / 12.0) ** d - 2.0 / n * disc1 +
                1.0 / (n ** 2) * disc2)
    elif method == 'WD':
        prod_arr = 1
        for i in range(d):
            s0 = sample[:, i]
            x_kikj = abs(s0[:, None] - s0)
            prod_arr *= 3.0 / 2.0 - x_kikj + x_kikj ** 2

        return - (4.0 / 3.0) ** d + 1.0 / (n ** 2) * prod_arr.sum()
    elif method == 'MD':
        abs_ = abs(sample - 0.5)
        disc1 = np.sum(np.prod(5.0 / 3.0 - 0.25 * abs_ - 0.25 * abs_ ** 2,
                               axis=1))

        prod_arr = 1
        for i in range(d):
            s0 = sample[:, i]
            prod_arr *= (15.0 / 8.0 -
                         0.25 * abs(s0[:, None] - 0.5) - 0.25 * abs(s0 - 0.5) -
                         3.0 / 4.0 * abs(s0[:, None] - s0) +
                         0.5 * abs(s0[:, None] - s0) ** 2)
        disc2 = prod_arr.sum()

        disc = (19.0 / 12.0) ** d
        disc1 = 2.0 / n * disc1
        disc2 = 1.0 / (n ** 2) * disc2

        return disc - disc1 + disc2
    elif method == 'star':
        return np.sqrt(
            3 ** (-d) - 2 ** (1 - d) / n
            * np.sum(np.prod(1 - sample ** 2, axis=1))
            + np.sum([
                np.prod(1 - np.maximum(sample[k, :], sample[j, :]))
                for k in range(n) for j in range(n)
            ]) / n ** 2
        )
    else:
        raise ValueError('{} is not a valid method. Options are '
                         'CD, WD, MD, star.'.format(method))


def _update_discrepancy(x_new, sample, initial_disc):
    """Update the centered discrepancy with a new sample.

    Parameters
    ----------
    x_new : array_like (1, d)
        The new sample to add in `sample`.
    sample : array_like (n, d)
        The initial sample.
    initial_disc : float
        Centered discrepancy of the `sample`.

    Returns
    -------
    discrepancy : float
        Centered discrepancy of the sample composed of `x_new` and `sample`.

    Examples
    --------
    We can also compute iteratively the discrepancy by using
    ``iterative=True``.

    >>> from scipy.stats import qmc
    >>> space = np.array([[1, 3], [2, 6], [3, 2], [4, 5], [5, 1], [6, 4]])
    >>> bounds = np.array([[0.5, 0.5], [6.5, 6.5]])
    >>> space = qmc.scale(space, bounds, reverse=True)
    >>> disc_init = qmc.discrepancy(space[:-1], iterative=True)
    >>> disc_init
    0.04769081147119336
    >>> _update_discrepancy(space[-1], space[:-1], disc_init) # doctest: +SKIP
    0.008142039609053513

    """
    sample = np.asarray(sample)
    x_new = np.asarray(x_new)

    n = len(sample) + 1
    abs_ = abs(x_new - 0.5)

    disc1 = - 2 / n * np.prod(1 + 1 / 2 * abs_ - 1 / 2 * abs_ ** 2)
    disc2 = 2 / (n ** 2) * np.sum(np.prod(1 + 1 / 2 * abs_ +
                                          1 / 2 * abs(sample - 0.5) -
                                          1 / 2 * abs(x_new - sample),
                                          axis=1))
    disc3 = 1 / (n ** 2) * np.prod(1 + abs_)

    return initial_disc + disc1 + disc2 + disc3


def _perturb_discrepancy(sample, i1, i2, k, disc):
    """Centered discrepancy after and elementary perturbation on a LHS.

    An elementary perturbation consists of an exchange of coordinates between
    two points: ``sample[i1, k] <-> sample[i2, k]``. By construction,
    this operation conserves the LHS properties.

    Parameters
    ----------
    sample : array_like (n, d)
        The sample (before permutation) to compute the discrepancy from.
    i1 : int
        The first line of the elementary permutation.
    i2 : int
        The second line of the elementary permutation.
    k : int
        The column of the elementary permutation.
    disc : float
        Centered discrepancy of the design before permutation.

    Returns
    -------
    discrepancy : float
        Centered discrepancy.

    References
    ----------
    .. [1] Jin et al. "An efficient algorithm for constructing optimal design
       of computer experiments", Journal of Statistical Planning and
       Inference, 2005.

    """
    sample = np.asarray(sample)
    n = sample.shape[0]

    z_ij = sample - 0.5

    # Eq (19)
    c_i1j = 1. / n ** 2. * np.prod(0.5 * (2. + abs(z_ij[i1, :]) +
                                          abs(z_ij) -
                                          abs(z_ij[i1, :] - z_ij)),
                                   axis=1)
    c_i2j = 1. / n ** 2. * np.prod(0.5 * (2. + abs(z_ij[i2, :]) +
                                          abs(z_ij) -
                                          abs(z_ij[i2, :] - z_ij)),
                                   axis=1)

    # Eq (20)
    c_i1i1 = (1. / n ** 2 * np.prod(1 + abs(z_ij[i1, :])) -
              2. / n * np.prod(1. + 0.5 * abs(z_ij[i1, :]) -
                               0.5 * z_ij[i1, :] ** 2))
    c_i2i2 = (1. / n ** 2 * np.prod(1 + abs(z_ij[i2, :])) -
              2. / n * np.prod(1. + 0.5 * abs(z_ij[i2, :]) -
                               0.5 * z_ij[i2, :] ** 2))

    # Eq (22), typo in the article in the denominator i2 -> i1
    num = (2 + abs(z_ij[i2, k]) + abs(z_ij[:, k]) -
           abs(z_ij[i2, k] - z_ij[:, k]))
    denum = (2 + abs(z_ij[i1, k]) + abs(z_ij[:, k]) -
             abs(z_ij[i1, k] - z_ij[:, k]))
    gamma = num / denum

    # Eq (23)
    c_p_i1j = gamma * c_i1j
    # Eq (24)
    c_p_i2j = c_i2j / gamma

    alpha = (1 + abs(z_ij[i2, k])) / (1 + abs(z_ij[i1, k]))
    beta = (2 - abs(z_ij[i2, k])) / (2 - abs(z_ij[i1, k]))

    g_i1 = np.prod(1. + abs(z_ij[i1, :]))
    g_i2 = np.prod(1. + abs(z_ij[i2, :]))
    h_i1 = np.prod(1. + 0.5 * abs(z_ij[i1, :]) - 0.5 * (z_ij[i1, :] ** 2))
    h_i2 = np.prod(1. + 0.5 * abs(z_ij[i2, :]) - 0.5 * (z_ij[i2, :] ** 2))

    # Eq (25), typo in the article g is missing
    c_p_i1i1 = ((g_i1 * alpha) / (n ** 2) - 2. * alpha * beta * h_i1 / n)
    # Eq (26), typo in the article n ** 2
    c_p_i2i2 = ((g_i2 / ((n ** 2) * alpha)) - (2. * h_i2 / (n * alpha * beta)))

    # Eq (26)
    sum_ = c_p_i1j - c_i1j + c_p_i2j - c_i2j

    mask = np.ones(n, dtype=bool)
    mask[[i1, i2]] = False
    sum_ = sum(sum_[mask])

    disc_ep = (disc + c_p_i1i1 - c_i1i1 + c_p_i2i2 - c_i2i2 + 2 * sum_)

    return disc_ep


def primes_from_2_to(n):
    """Prime numbers from 2 to *n*.

    Parameters
    ----------
    n : int
        Sup bound with ``n >= 6``.

    Returns
    -------
    primes : list(int)
        Primes in ``2 <= p < n``.

    References
    ----------
    .. [1] `StackOverflow <https://stackoverflow.com/questions/2068372>`_.

    """
    sieve = np.ones(n // 3 + (n % 6 == 2), dtype=bool)
    for i in range(1, int(n ** 0.5) // 3 + 1):
        k = 3 * i + 1 | 1
        sieve[k * k // 3::2 * k] = False
        sieve[k * (k - 2 * (i & 1) + 4) // 3::2 * k] = False
    return np.r_[2, 3, ((3 * np.nonzero(sieve)[0][1:] + 1) | 1)]


def n_primes(n):
    """List of the n-first prime numbers.

    Parameters
    ----------
    n : int
        Number of prime numbers wanted.

    Returns
    -------
    primes : list(int)
        List of primes.

    """
    primes = [2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41, 43, 47, 53, 59,
              61, 67, 71, 73, 79, 83, 89, 97, 101, 103, 107, 109, 113, 127,
              131, 137, 139, 149, 151, 157, 163, 167, 173, 179, 181, 191, 193,
              197, 199, 211, 223, 227, 229, 233, 239, 241, 251, 257, 263, 269,
              271, 277, 281, 283, 293, 307, 311, 313, 317, 331, 337, 347, 349,
              353, 359, 367, 373, 379, 383, 389, 397, 401, 409, 419, 421, 431,
              433, 439, 443, 449, 457, 461, 463, 467, 479, 487, 491, 499, 503,
              509, 521, 523, 541, 547, 557, 563, 569, 571, 577, 587, 593, 599,
              601, 607, 613, 617, 619, 631, 641, 643, 647, 653, 659, 661, 673,
              677, 683, 691, 701, 709, 719, 727, 733, 739, 743, 751, 757, 761,
              769, 773, 787, 797, 809, 811, 821, 823, 827, 829, 839, 853, 857,
              859, 863, 877, 881, 883, 887, 907, 911, 919, 929, 937, 941, 947,
              953, 967, 971, 977, 983, 991, 997][:n]

    if len(primes) < n:
        big_number = 2000
        while 'Not enough primes':
            primes = primes_from_2_to(big_number)[:n]
            if len(primes) == n:
                break
            big_number += 1000

    return primes


def van_der_corput(n, base=2, start_index=0, scramble=False, seed=None):
    """Van der Corput sequence.

    Pseudo-random number generator based on a b-adic expansion.

    Scrambling uses permutations of the remainders (see [1]_). Multiple
    permutations are applied to construct a point. The sequence of
    permutations has to be the same for all points of the sequence.

    Parameters
    ----------
    n : int
        Number of element of the sequence.
    base : int, optional
        Base of the sequence. Default is 2.
    start_index : int, optional
        Index to start the sequence from. Default is 0.
    scramble: bool, optional
        If True, use Owen scrambling. Otherwise no scrambling is done.
        Default is True.
    seed : {None, int, `numpy.random.Generator`}, optional
        If `seed` is None the `numpy.random.Generator` singleton is used.
        If `seed` is an int, a new ``Generator`` instance is used,
        seeded with `seed`.
        If `seed` is already a ``Generator`` instance then that instance is
        used.

    Returns
    -------
    sequence : list (n,)
        Sequence of Van der Corput.

    References
    ----------
    .. [1] A. B. Owen. "A randomized Halton algorithm in R",
       arXiv:1706.02808, 2017.

    """
    rng = check_random_state(seed)
    sequence = np.zeros(n)

    quotient = np.arange(start_index, start_index + n)
    b2r = 1 / base

    while (1 - b2r) < 1:
        remainder = quotient % base

        if scramble:
            # permutation must be the same for all points of the sequence
            perm = rng.permutation(base)
            remainder = perm[np.array(remainder).astype(int)]

        sequence += remainder * b2r
        b2r /= base
        quotient = (quotient - remainder) / base

    return sequence


class QMCEngine(ABC):
    """A generic Quasi-Monte Carlo sampler class meant for subclassing.

    QMCEngine is a base class to construct a specific Quasi-Monte Carlo sampler.
    It cannot be used directly as a sampler.

    Parameters
    ----------
    d : int
        Dimension of the parameter space.
    seed : {None, int, `numpy.random.Generator`}, optional
        If `seed` is None the `numpy.random.Generator` singleton is used.
        If `seed` is an int, a new ``Generator`` instance is used,
        seeded with `seed`.
        If `seed` is already a ``Generator`` instance then that instance is
        used.

    Notes
    -----
    By convention samples are distributed over the half-open interval
    ``[0, 1)``. Instances of the class can access the attributes: ``d`` for
    the dimension; and ``rng`` for the random number generator (used for the
    ``seed``).

    **Subclassing**

    When subclassing `QMCEngine` to create a new sampler,  ``__init__`` and
    ``random`` must be redefined.

    * ``__init__(d, seed=None)``: at least fix the dimension. If the sampler
      does not take advantage of a ``seed`` (deterministic methods like
      Halton), this parameter can be omitted.
    * ``random(n)``: draw ``n`` from the engine.

    Optionally, two other methods can be overwritten by subclasses:

    * ``reset``: Reset the engine to it's original state.
    * ``fast_forward``: If the sequence is deterministic (like Halton
      sequence), then ``fast_forward(n)`` is skipping the ``n`` first draw.

    Examples
    --------
    To create a random sampler based on ``np.random.random``, we would do the
    following:

    >>> from scipy.stats import qmc
    >>> class RandomEngine(qmc.QMCEngine):
    ...     def __init__(self, d, seed):
    ...         super().__init__(d=d, seed=seed)
    ...         self.rng_seed = seed
    ...
    ...
    ...     def random(self, n=1):
    ...         return self.rng.random((n, self.d))
    ...
    ...
    ...     def reset(self):
    ...         super().__init__(d=self.d, seed=self.rng_seed)
    ...         return self
    ...
    ...
    ...     def fast_forward(self, n):
    ...         self.rng.random((n, self.d))
    ...         return self

    After subclassing `QMCEngine` to define the sampling strategy we want to use,
    we can create an instance to sample from.

    >>> engine = RandomEngine(2, seed=12345)
    >>> engine.random(5)
    array([[0.22733602, 0.31675834],
           [0.79736546, 0.67625467],
           [0.39110955, 0.33281393],
           [0.59830875, 0.18673419],
           [0.67275604, 0.94180287]])

    We can also reset the state of the generator and resample again.

    >>> _ = engine.reset()
    >>> engine.random(5)
    array([[0.22733602, 0.31675834],
           [0.79736546, 0.67625467],
           [0.39110955, 0.33281393],
           [0.59830875, 0.18673419],
           [0.67275604, 0.94180287]])

    """

    @abstractmethod
    def __init__(self, d, seed=None):
        self.d = d
        self.rng = check_random_state(seed)
        self.rng_seed = copy.deepcopy(seed)
        self.num_generated = 0

    @abstractmethod
    def random(self, n=1):
        """Draw `n` in the half-open interval ``[0, 1)``.

        Parameters
        ----------
        n : int, optional
            Number of samples to generate in the parameter space.
            Default is 1.

        Returns
        -------
        sample : array_like (n, d)
            QMC sample.

        """
        # self.num_generated += n

    def reset(self):
        """Reset the engine to base state.

        Returns
        -------
        engine: QMCEngine
            Engine reset to its base state.

        """
        self.rng = check_random_state(self.rng_seed)
        self.num_generated = 0
        return self

    def fast_forward(self, n):
        """Fast-forward the sequence by `n` positions.

        Parameters
        ----------
        n: int
            Number of points to skip in the sequence.

        Returns
        -------
        engine: QMCEngine
            Engine reset to its base state.

        """
        self.num_generated += n
        return self


class Halton(QMCEngine):
    """Halton sequence.

    Pseudo-random number generator that generalize the Van der Corput sequence
    for multiple dimensions. The Halton sequence uses the base-two Van der Corput
    sequence for the first dimension, base-three for its second and base-:math:`n` for
    its n-dimension.

    Parameters
    ----------
    d : int
        Dimension of the parameter space.
    scramble: bool, optional
        If True, use Owen scrambling. Otherwise no scrambling is done.
        Default is True.
    seed : {None, int, `numpy.random.Generator`}, optional
        If `seed` is None the `numpy.random.Generator` singleton is used.
        If `seed` is an int, a new ``Generator`` instance is used,
        seeded with `seed`.
        If `seed` is already a ``Generator`` instance then that instance is
        used.

    Notes
    -----
    The Halton sequence has severe striping artifacts for even modestly
    large dimensions. These can be ameliorated by scrambling. Scrambling
    also supports replication-based error estimates and extends
    applicabiltiy to unbounded integrands.

    References
    ----------
    .. [1] Halton, "On the efficiency of certain quasi-random sequences of
       points in evaluating multi-dimensional integrals", Numerische
       Mathematik, 1960.
    .. [2] A. B. Owen. "A randomized Halton algorithm in R",
       arXiv:1706.02808, 2017.

    Examples
    --------
    Generate samples from a low discrepancy sequence of Halton.

    >>> from scipy.stats import qmc
    >>> sampler = qmc.Halton(d=2, scramble=False)
    >>> sample = sampler.random(n=5)
    >>> sample
    array([[0.        , 0.        ],
           [0.5       , 0.33333333],
           [0.25      , 0.66666667],
           [0.75      , 0.11111111],
           [0.125     , 0.44444444]])

    Compute the quality of the sample using the discrepancy criterion.

    >>> qmc.discrepancy(sample)
    0.088893711419753

    If some wants to continue an existing design, extra points can be obtained
    by calling again `random`. Alternatively, you can skip some points like:

    >>> _ = sampler.fast_forward(5)
    >>> sample_continued = sampler.random(n=5)
    >>> sample_continued
    array([[0.3125    , 0.37037037],
           [0.8125    , 0.7037037 ],
           [0.1875    , 0.14814815],
           [0.6875    , 0.48148148],
           [0.4375    , 0.81481481]])

    Finally, samples can be scaled to bounds.

    >>> bounds = [[0, 2], [10, 5]]
    >>> qmc.scale(sample_continued, bounds)
    array([[3.125     , 3.11111111],
           [8.125     , 4.11111111],
           [1.875     , 2.44444444],
           [6.875     , 3.44444444],
           [4.375     , 4.44444444]])

    """

    def __init__(self, d, scramble=True, seed=None):
        super().__init__(d=d, seed=seed)
        self.seed = seed
        self.base = n_primes(d)
        self.scramble = scramble

    def random(self, n=1):
        """Draw `n` in the half-open interval ``[0, 1)``.

        Parameters
        ----------
        n : int, optional
            Number of samples to generate in the parameter space. Default is 1.

        Returns
        -------
        sample : array_like (n, d)
            QMC sample.

        """
        # Generate a sample using a Van der Corput sequence per dimension.
        # important to have ``type(bdim) == int`` for performance reason
        sample = [van_der_corput(n, int(bdim), self.num_generated,
                                 scramble=self.scramble,
                                 seed=copy.deepcopy(self.seed))
                  for bdim in self.base]

        self.num_generated += n
        return np.array(sample).T.reshape(n, self.d)


class OrthogonalLatinHypercube(QMCEngine):
    """Orthogonal array-based Latin hypercube sampling (OA-LHS).

    In addition to the constraints from the Latin Hypercube, an orthogonal
    array of size `n` is defined and only one point is allowed per subspace.

    Parameters
    ----------
    d : int
        Dimension of the parameter space.
    seed : {None, int, `numpy.random.Generator`}, optional
        If `seed` is None the `numpy.random.Generator` singleton is used.
        If `seed` is an int, a new ``Generator`` instance is used,
        seeded with `seed`.
        If `seed` is already a ``Generator`` instance then that instance is
        used.

    References
    ----------
    .. [1] Art B. Owen, "Orthogonal arrays for computer experiments,
       integration and visualization", Statistica Sinica, 1992.

    Examples
    --------
    Generate samples from an orthogonal latin hypercube generator.

    >>> from scipy.stats import qmc
    >>> sampler = qmc.OrthogonalLatinHypercube(d=2, seed=12345)
    >>> sample = sampler.random(n=5)
    >>> sample
    array([[0.0454672 , 0.58836057],
           [0.55947309, 0.03734684],
           [0.26335167, 0.98977623],
           [0.87822191, 0.33455121],
           [0.73525093, 0.64964914]])

    Compute the quality of the sample using the discrepancy criterion.

    >>> qmc.discrepancy(sample)
    0.02050567122966518

    Finally, samples can be scaled to bounds.

    >>> bounds = [[0, 2], [10, 5]]
    >>> qmc.scale(sample, bounds)
    array([[0.45467204, 3.76508172],
           [5.59473091, 2.11204051],
           [2.63351668, 4.96932869],
           [8.7822191 , 3.00365363],
           [7.35250934, 3.94894743]])

    """

    def __init__(self, d, seed=None):
        super().__init__(d=d, seed=seed)

    def random(self, n=1):
        """Draw `n` in the half-open interval ``[0, 1)``.

        Parameters
        ----------
        n : int, optional
            Number of samples to generate in the parameter space. Default is 1.

        Returns
        -------
        sample : array_like (n, d)
            OLHS sample.

        """
        sample = []
        step = 1.0 / n

        for _ in range(self.d):
            # Enforce a unique point per grid
            j = np.arange(n) * step
            temp = j + self.rng.uniform(low=0, high=step, size=n)
            self.rng.shuffle(temp)

            sample.append(temp)

        self.num_generated += n
        return np.array(sample).T.reshape(n, self.d)


class LatinHypercube(QMCEngine):
    """Latin hypercube sampling (LHS).

    A Latin hypercube sample [1]_ generates :math:`n` points in
    :math:`[0,1)^{d}`. Each univariate marginal distribution is stratified,
    placing exactly one point in :math:`[j/n, (j+1)/n)` for
    :math:`j=0,1,...,n-1`. They are still applicable when :math:`n << d`.
    LHS is extremely effective on integrands that are nearly additive [2]_.
    LHS on :math:`n` points never has more variance than plain MC on
    :math:`n-1` points [3]_. There is a central limit theorem for plain
    LHS [4]_, but not necessarily for optimized LHS.

    Parameters
    ----------
    d : int
        Dimension of the parameter space.
    centered : bool, optional
        Center the point within the multi-dimensional grid. Default is False.
    seed : {None, int, `numpy.random.Generator`}, optional
        If `seed` is None the `numpy.random.Generator` singleton is used.
        If `seed` is an int, a new ``Generator`` instance is used,
        seeded with `seed`.
        If `seed` is already a ``Generator`` instance then that instance is
        used.

    References
    ----------
    .. [1] Mckay et al., "A Comparison of Three Methods for Selecting Values
       of Input Variables in the Analysis of Output from a Computer Code",
       Technometrics, 1979.
    .. [2] M. Stein, "Large sample properties of simulations using Latin
       hypercube sampling." Technometrics 29, no. 2: 143-151, 1987.
    .. [3] A. B. Owen, "Monte Carlo variance of scrambled net quadrature."
       SIAM Journal on Numerical Analysis 34, no. 5: 1884-1910, 1997
    .. [4]  Loh, W.-L. "On Latin hypercube sampling." The annals of statistics
       24, no. 5: 2058-2080, 1996.

    Examples
    --------
    Generate samples from a Latin hypercube generator.

    >>> from scipy.stats import qmc
    >>> sampler = qmc.LatinHypercube(d=2, seed=12345)
    >>> sample = sampler.random(n=5)
    >>> sample
    array([[0.5545328 , 0.13664833],
           [0.64052691, 0.66474907],
           [0.52177809, 0.53343721],
           [0.08033825, 0.16265316],
           [0.26544879, 0.21163943]])

    Compute the quality of the sample using the discrepancy criterion.

    >>> qmc.discrepancy(sample)
    0.07254149611314986

    Finally, samples can be scaled to bounds.

    >>> bounds = [[0, 2], [10, 5]]
    >>> qmc.scale(sample, bounds)
    array([[5.54532796, 2.409945  ],
           [6.40526909, 3.9942472 ],
           [5.2177809 , 3.60031164],
           [0.80338249, 2.48795949],
           [2.65448791, 2.63491828]])

    """

    def __init__(self, d, centered=False, seed=None):
        super().__init__(d=d, seed=seed)
        self.centered = centered

        # This can be removed once numpy 1.16 is dropped
        try:
            self.rg_integers = self.rng.randint
            self.rg_sample = self.rng.random_sample
        except AttributeError:
            self.rg_integers = self.rng.integers
            self.rg_sample = self.rng.random

    def random(self, n=1):
        """Draw `n` in the half-open interval ``[0, 1)``.

        Parameters
        ----------
        n : int, optional
            Number of samples to generate in the parameter space. Default is 1.

        Returns
        -------
        sample : array_like (n, d)
            LHS sample.

        """
        if self.centered:
            r = 0.5
        else:
            r = self.rg_sample((n, self.d))

        q = self.rg_integers(low=1, high=n, size=(n, self.d))

        self.num_generated += n
        return 1. / n * (q - r)

    def reset(self):
        """Reset the engine to base state.

        Returns
        -------
        engine: LatinHypercube
            Engine reset to its base state.

        """
        self.__init__(d=self.d, centered=self.centered, seed=self.rng_seed)
        self.num_generated = 0
        return self


class OptimalDesign(QMCEngine):
    """Optimal design.

    Optimize the design by doing random permutations to lower the centered
    discrepancy.

    The specified optimization `method` is used to select a new set of
    permutations to perform. If `method` is None, *basinhopping* optimization
    is used. `niter` set of permutations are performed.

    Centered discrepancy-based design shows better space filling robustness
    toward 2D and 3D subprojections. Distance-based design shows better space
    filling but less robustness to subprojections.

    Parameters
    ----------
    d : int
        Dimension of the parameter space.
    start_design : array_like (n, d), optional
        Initial design of experiment to optimize.
    niter : int, optional
        Number of iterations to perform. Default is 1.
    method : callable ``f(func, x0, bounds)``, optional
        Optimization function used to search new samples. Default to
        *basinhopping* optimization.
    seed : {None, int, `numpy.random.Generator`}, optional
        If `seed` is None the `numpy.random.Generator` singleton is used.
        If `seed` is an int, a new ``Generator`` instance is used,
        seeded with `seed`.
        If `seed` is already a ``Generator`` instance then that instance is
        used.

    References
    ----------
    .. [1] Fang et al. Design and modeling for computer experiments,
       Computer Science and Data Analysis Series, 2006.
    .. [2] Damblin et al., "Numerical studies of space filling designs:
       optimization of Latin Hypercube Samples and subprojection properties",
       Journal of Simulation, 2013.

    Examples
    --------
    Generate samples from an optimal design.

    >>> from scipy.stats import qmc
    >>> sampler = qmc.OptimalDesign(d=2, seed=12345)
    >>> sample = sampler.random(n=5)
    >>> sample
    array([[0.0454672 , 0.58836057],
           [0.55947309, 0.98977623],
           [0.26335167, 0.03734684],
           [0.87822191, 0.33455121],
           [0.73525093, 0.64964914]])

    Compute the quality of the sample using the discrepancy criterion.

    >>> qmc.discrepancy(sample)
    0.018581537720176344

    You can possibly improve the quality of the sample by performing more
    optimization iterations by using `niter`:

    >>> sampler_2 = qmc.OptimalDesign(d=2, niter=5, seed=12345)
    >>> sample_2 = sampler_2.random(n=5)
    >>> qmc.discrepancy(sample_2)
    0.018378401228740238

    Finally, samples can be scaled to bounds.

    >>> bounds = [[0, 2], [10, 5]]
    >>> qmc.scale(sample, bounds)
    array([[0.45467204, 3.76508172],
           [5.59473091, 4.96932869],
           [2.63351668, 2.11204051],
           [8.7822191 , 3.00365363],
           [7.35250934, 3.94894743]])

    """

    def __init__(self, d, start_design=None, niter=1, method=None, seed=None):
        super().__init__(d=d, seed=seed)
        self.start_design = start_design
        self.niter = niter

        if method is None:
            def method(func, x0, bounds):
                """Basinhopping optimization."""
                minimizer_kwargs = {"method": "L-BFGS-B", "bounds": bounds}
                basinhopping(func, x0, niter=100,
                             minimizer_kwargs=minimizer_kwargs, seed=self.rng)

        self.method = method

        self.best_doe = self.start_design
        if self.start_design is not None:
            self.best_disc = discrepancy(self.start_design)
        else:
            self.best_disc = np.inf

        self.olhs = OrthogonalLatinHypercube(self.d, seed=self.rng)

    def random(self, n=1):
        """Draw `n` in the half-open interval ``[0, 1)``.

        Parameters
        ----------
        n : int, optional
            Number of samples to generate in the parameter space. Default is 1.

        Returns
        -------
        sample : array_like (n, d)
            Optimal sample.

        """
        if self.d == 0:
            return np.empty((n, 0))

        if self.best_doe is None:
            self.best_doe = self.olhs.random(n)
            self.best_disc = discrepancy(self.best_doe)

        def _perturb_best_doe(x: np.ndarray) -> float:
            """Perturb the DoE and keep track of the best DoE.

            Parameters
            ----------
            x : list of int
                It is a list of:
                    idx : int
                        Index value of the components to compute

            Returns
            -------
            discrepancy : float
                Centered discrepancy.

            """
            # Perturb the DoE
            doe = self.best_doe.copy()
            col, row_1, row_2 = np.round(x).astype(int)

            disc = _perturb_discrepancy(self.best_doe, row_1, row_2, col,
                                        self.best_disc)
            if disc < self.best_disc:
                doe[row_1, col], doe[row_2, col] = doe[row_2, col], \
                                                   doe[row_1, col]
                self.best_disc = disc
                self.best_doe = doe

            return disc

        x0 = [0, 0, 0]
        bounds = ([0, self.d - 1],
                  [0, n - 1],
                  [0, n - 1])

        for _ in range(self.niter):
            self.method(_perturb_best_doe, x0, bounds)

        self.num_generated += n
        return self.best_doe

    def reset(self):
        """Reset the engine to base state.

        Returns
        -------
        engine: OptimalDesign
            Engine reset to its base state.

        """
        self.__init__(d=self.d, seed=self.rng_seed)
        self.num_generated = 0
        return self


class Sobol(QMCEngine):
    """Engine for generating (scrambled) Sobol' sequences.

    Sobol' sequences are low-discrepancy, quasi-random numbers. Points
    can be drawn using two methods:

    * `random_base2`: safely draw :math:`n=2^m` points. This method
      guarantees the balance properties of the sequence.
    * `random`: draw an arbitrary number of points from the
      sequence. See warning below.

    Parameters
    ----------
    d : int
        Dimensionality of the sequence. Max dimensionality is 21201.
    scramble : bool, optional
        If True, use Owen scrambling. Otherwise no scrambling is done.
        Default is True.
    seed : {None, int, `numpy.random.Generator`}, optional
        If `seed` is None the `numpy.random.Generator` singleton is used.
        If `seed` is an int, a new ``Generator`` instance is used,
        seeded with `seed`.
        If `seed` is already a ``Generator`` instance then that instance is
        used.

    Notes
    -----
    Sobol' sequences [1]_ provide :math:`n=2^m` low discrepancy points in
    :math:`[0,1)^{d}`. Scrambling them [2]_ makes them suitable for singular
    integrands, provides a means of error estimation, and can improve their
    rate of convergence.

    There are many versions of Sobol' sequences depending on their
    'direction numbers'. This code uses direction numbers from [3]_. Hence,
    the maximum number of dimension is 21201. The direction numbers have been
    precomputed with search criterion 6 and can be retrieved at
    https://web.maths.unsw.edu.au/~fkuo/sobol/.

    .. warning::

       Sobol' sequences are a quadrature rule and they lose their balance
       properties if one uses a sample size that is not a power of 2, or skips
       the first point, or thins the sequence [4]_.

       If :math:`n=2^m` points are not enough then one should take :math:`2^M`
       points for :math:`M>m`. When scrambling, the number R of independent
       replicates does not have to be a power of 2.

       Sobol' sequences are generated to some number :math:`B` of bits.
       After :math:`2^B` points have been generated, the sequence will repeat.
       Currently :math:`B=30`.

    References
    ----------
    .. [1] I. M. Sobol. The distribution of points in a cube and the accurate
       evaluation of integrals. Zh. Vychisl. Mat. i Mat. Phys., 7:784-802,
       1967.

    .. [2] Art B. Owen. Scrambling Sobol and Niederreiter-Xing points.
       Journal of Complexity, 14(4):466-489, December 1998.

    .. [3] S. Joe and F. Y. Kuo. Constructing sobol sequences with better
       two-dimensional projections. SIAM Journal on Scientific Computing,
       30(5):2635-2654, 2008.

    .. [4] Art B. Owen. On dropping the first Sobol' point. arXiv 2008.08051,
       2020.

    Examples
    --------
    Generate samples from a low discrepancy sequence of Sobol'.

    >>> from scipy.stats import qmc
    >>> sampler = qmc.Sobol(d=2, scramble=False)
    >>> sample = sampler.random_base2(m=3)
    >>> sample
    array([[0.   , 0.   ],
           [0.5  , 0.5  ],
           [0.75 , 0.25 ],
           [0.25 , 0.75 ],
           [0.375, 0.375],
           [0.875, 0.875],
           [0.625, 0.125],
           [0.125, 0.625]])

    Compute the quality of the sample using the discrepancy criterion.

    >>> qmc.discrepancy(sample)
    0.013882107204860938

    To continue an existing design, extra points can be obtained
    by calling again `random_base2`. Alternatively, you can skip some
    points like:

    >>> _ = sampler.reset()
    >>> _ = sampler.fast_forward(4)
    >>> sample_continued = sampler.random_base2(m=2)
    >>> sample_continued
    array([[0.375, 0.375],
           [0.875, 0.875],
           [0.625, 0.125],
           [0.125, 0.625]])

    Finally, samples can be scaled to bounds.

    >>> bounds = [[0, 2], [10, 5]]
    >>> qmc.scale(sample_continued, bounds)
    array([[3.75 , 3.125],
           [8.75 , 4.625],
           [6.25 , 2.375],
           [1.25 , 3.875]])

    """

    MAXDIM = _MAXDIM
    MAXBIT = _MAXBIT

    def __init__(self, d, scramble=True, seed=None):
        if d > self.MAXDIM:
            raise ValueError(
                "Maximum supported dimensionality is {}.".format(self.MAXDIM)
            )
        super().__init__(d=d, seed=seed)

        # initialize direction numbers
        initialize_direction_numbers()

        # v is d x MAXBIT matrix
        self._sv = np.zeros((d, self.MAXBIT), dtype=int)
        initialize_v(self._sv, d)

        if not scramble:
            self._shift = np.zeros(d, dtype=int)
        else:
            self._scramble()

        self._quasi = self._shift.copy()
        self._first_point = (self._quasi / 2 ** self.MAXBIT).reshape(1, -1)

    def _scramble(self):
        """Scramble the sequence."""
        try:
            rg_integers = self.rng.integers
        except AttributeError:
            rg_integers = self.rng.randint
        # Generate shift vector
        self._shift = np.dot(
            rg_integers(2, size=(self.d, self.MAXBIT)),
            2 ** np.arange(self.MAXBIT),
        )
        self._quasi = self._shift.copy()
        # Generate lower triangular matrices (stacked across dimensions)
        ltm = np.tril(rg_integers(2, size=(self.d, self.MAXBIT, self.MAXBIT)))
        _cscramble(self.d, ltm, self._sv)
        self.num_generated = 0

    def random(self, n=1):
        """Draw next point(s) in the Sobol' sequence.

        Parameters
        ----------
        n : int, optional
            Number of samples to generate in the parameter space. Default is 1.

        Returns
        -------
        sample : array_like (n, d)
            Sobol' sample.

        """
        sample = np.empty((n, self.d), dtype=float)

        if self.num_generated == 0:
            # verify n is 2**n
            if not (n & (n - 1) == 0):
                warnings.warn("The balance properties of Sobol' points require"
                              " n to be a power of 2.")

            if n == 1:
                sample = self._first_point
            else:
                _draw(n - 1, self.num_generated, self.d, self._sv,
                      self._quasi, sample)
                sample = np.concatenate([self._first_point, sample])[:n]
        else:
            _draw(n, self.num_generated - 1, self.d, self._sv,
                  self._quasi, sample)

        self.num_generated += n
        return sample

    def random_base2(self, m):
        """Draw point(s) from the Sobol' sequence.

        This function draws :math:`n=2^m` points in the parameter space
        ensuring the balance properties of the sequence.

        Parameters
        ----------
        m : int
            Logarithm in base 2 of the number of samples; i.e., n = 2^m.

        Returns
        -------
        sample : array_like (n, d)
            Sobol' sample.

        """
        n = 2 ** m

        total_n = self.num_generated + n
        if not (total_n & (total_n - 1) == 0):
            raise ValueError("The balance properties of Sobol' points require "
                             "n to be a power of 2. {0} points have been "
                             "previously generated, then: n={0}+2**{1}={2}. "
                             "If you still want to do this, the function "
                             "'Sobol.random()' can be used."
                             .format(self.num_generated, m, total_n))

        return self.random(n)

    def reset(self):
        """Reset the engine to base state.

        Returns
        -------
        engine: Sobol
            Engine reset to its base state.

        """
        self._quasi = self._shift.copy()
        self.num_generated = 0
        return self

    def fast_forward(self, n):
        """Fast-forward the sequence by `n` positions.

        Parameters
        ----------
        n: int
            Number of points to skip in the sequence.

        Returns
        -------
        engine: Sobol
            The fast-forwarded engine.

        """
        if self.num_generated == 0:
            _fast_forward(n - 1, self.num_generated, self.d,
                          self._sv, self._quasi)
        else:
            _fast_forward(n, self.num_generated - 1, self.d,
                          self._sv, self._quasi)
        self.num_generated += n
        return self


class MultivariateNormalQMC(QMCEngine):
    r"""QMC sampling from a multivariate Normal :math:`N(\mu, \Sigma)`.

    Parameters
    ----------
    mean: array_like (d,)
        The mean vector.
    cov: array_like (d, d), optional
        The covariance matrix. If omitted, use `cov_root` instead.
        If both `cov` and `cov_root` are omitted, use the identity matrix.
    cov_root: array_like (d, d'), optional
        A root decomposition of the covariance matrix, where `d'` may be less
        than `d` if the covariance is not full rank. If omitted, use `cov`.
    inv_transform: bool, optional
        If True, use inverse transform instead of Box-Muller. Default is True.
    engine: QMCEngine, optional
        Quasi-Monte Carlo engine sampler. If None, Sobol' is used.
    seed : {None, int, `numpy.random.Generator`}, optional
        If `seed` is None the `numpy.random.Generator` singleton is used.
        If `seed` is an int, a new ``Generator`` instance is used,
        seeded with `seed`.
        If `seed` is already a ``Generator`` instance then that instance is
        used.

    Examples
    --------
    >>> import matplotlib.pyplot as plt
    >>> from scipy.stats import qmc
    >>> engine = qmc.MultivariateNormalQMC(mean=[0, 5], cov=[[1, 0], [0, 1]])
    >>> sample = engine.random(512)
    >>> _ = plt.scatter(sample[:, 0], sample[:, 1])
    >>> plt.show()

    """

    def __init__(self, mean, cov=None, cov_root=None, inv_transform=True,
                 engine=None, seed=None):
        mean = np.array(mean, copy=False, ndmin=1)
        d = mean.shape[0]
        if cov is not None:
            # covariance matrix provided
            cov = np.array(cov, copy=False, ndmin=2)
            # check for square/symmetric cov matrix and mean vector has the same d
            if not mean.shape[0] == cov.shape[0]:
                raise ValueError("Dimension mismatch between mean and covariance.")
            if not np.allclose(cov, cov.transpose()):
                raise ValueError("Covariance matrix is not symmetric.")
            # compute Cholesky decomp; if it fails, do the eigen decomposition
            try:
                cov_root = np.linalg.cholesky(cov).transpose()
            except np.linalg.LinAlgError:
                eigval, eigvec = np.linalg.eigh(cov)
                if not np.all(eigval >= -1.0e-8):
                    raise ValueError("Covariance matrix not PSD.")
                eigval = np.clip(eigval, 0.0, None)
                cov_root = (eigvec * np.sqrt(eigval)).transpose()
        elif cov_root is not None:
            # root decomposition provided
            cov_root = np.array(cov_root, copy=False, ndmin=2)
            if not mean.shape[0] == cov_root.shape[0]:
                raise ValueError("Dimension mismatch between mean and covariance.")
        else:
            # corresponds to identity covariance matrix
            cov_root = None

        super().__init__(d=d, seed=seed)
        self._inv_transform = inv_transform

        if not inv_transform:
            # to apply Box-Muller, we need an even number of dimensions
            engine_dim = 2 * math.ceil(d / 2)
        else:
            engine_dim = d
        if engine is None:
            self.engine = Sobol(d=engine_dim, scramble=True, seed=seed)
        else:
            self.engine = engine
        self._mean = mean
        self._corr_matrix = cov_root

    def random(self, n=1):
        """Draw `n` QMC samples from the multivariate Normal.

        Parameters
        ----------
        n : int, optional
            Number of samples to generate in the parameter space. Default is 1.

        Returns
        -------
        sample : array_like (n, d)
            Sample.

        """
        base_samples = self._standard_normal_samples(n)
        return self._correlate(base_samples)

    def reset(self):
        """Reset the engine to base state.

        Returns
        -------
        engine: MultivariateNormalQMC
            Engine reset to its base state.

        """
        self.engine.reset()
        return self

    def _correlate(self, base_samples):
        if self._corr_matrix is not None:
            return base_samples @ self._corr_matrix + self._mean
        else:
            # avoid mulitplying with identity here
            return base_samples + self._mean

    def _standard_normal_samples(self, n=1):
        """Draw `n` QMC samples from the standard Normal N(0, I_d).

        Parameters
        ----------
        n : int, optional
            Number of samples to generate in the parameter space. Default is 1.

        Returns
        -------
        sample : array_like (n, d)
            Sample.

        """
        # get base samples
        samples = self.engine.random(n)
        if self._inv_transform:
            # apply inverse transform
            # (values to close to 0/1 result in inf values)
            return norm.ppf(0.5 + (1 - 1e-10) * (samples - 0.5))
        else:
            # apply Box-Muller transform (note: indexes starting from 1)
            even = np.arange(0, samples.shape[-1], 2)
            Rs = np.sqrt(-2 * np.log(samples[:, even]))
            thetas = 2 * math.pi * samples[:, 1 + even]
            cos = np.cos(thetas)
            sin = np.sin(thetas)
            transf_samples = np.stack([Rs * cos, Rs * sin],
                                      -1).reshape(n, -1)
            # make sure we only return the number of dimension requested
            return transf_samples[:, : self.d]


class MultinomialQMC(QMCEngine):
    r"""QMC sampling from a multinomial distribution.

    Parameters
    ----------
    pvals: Iterable[float]
        float vector of probabilities of size `k`, where `k` is the number of
        categories. Elements must be non-negative and sum to 1.
    engine: QMCEngine, optional
        Quasi-Monte Carlo engine sampler. If None, Sobol is used.
    seed : {None, int, `numpy.random.Generator`}, optional
        If `seed` is None the `numpy.random.Generator` singleton is used.
        If `seed` is an int, a new ``Generator`` instance is used,
        seeded with `seed`.
        If `seed` is already a ``Generator`` instance then that instance is
        used.

    Examples
    --------
    >>> from scipy.stats import qmc
    >>> engine = qmc.MultinomialQMC(pvals=[0.2, 0.4, 0.4])
    >>> sample = engine.random(10)

    """

    def __init__(self, pvals, engine=None, seed=None):
        self.pvals = np.array(pvals, copy=False, ndmin=1)
        if np.min(pvals) < 0:
            raise ValueError('Elements of pvals must be non-negative.')
        if not np.isclose(np.sum(pvals), 1):
            raise ValueError('Elements of pvals must sum to 1.')
        if engine is None:
            engine = Sobol(d=1, scramble=True, seed=seed)
        self.engine = engine

    def random(self, n=1):
        """Draw `n` QMC samples from the multinomial distribution.

        Parameters
        ----------
        n : int, optional
            Number of samples to generate in the parameter space. Default is 1.

        Returns
        -------
        samples: array_like (pvals,)
            int vector of size ``p`` summing to `n`.

        """
        base_draws = self.engine.random(n).ravel()
        p_cumulative = np.empty_like(self.pvals, dtype=float)
        _fill_p_cumulative(np.array(self.pvals, dtype=float), p_cumulative)
        sample = np.zeros_like(self.pvals, dtype=int)
        _categorize(base_draws, p_cumulative, sample)
        return sample

    def reset(self):
        """Reset the engine to base state.

        Returns
        -------
        engine: MultinomialQMC
            Engine reset to its base state.

        """
        self.engine.reset()
        return self
