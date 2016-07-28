import numpy as np
import scipy.sparse as sparse
import warnings
    
DEFAULT_MU = 1e-6
DEFAULT_RATE = 0.01
DEFAULT_ALPHA = 0.5

def log_odds(p):
  """This is the logit function"""
  return np.log(p / (1.0 - p))

def odds_to_prob(l):
  """
  This is the inverse logit function logit^{-1}:

    l       = \log\frac{p}{1-p}
    \exp(l) = \frac{p}{1-p}
    p       = \frac{\exp(l)}{1 + \exp(l)}
  """
  return np.exp(l) / (1.0 + np.exp(l))

def sample_data(X, w, n_samples):
  """
  Here we do Gibbs sampling over the decision variables (representing our objects), o_j
  corresponding to the columns of X
  The model is just logistic regression, e.g.

    P(o_j=1 | X_{*,j}; w) = logit^{-1}(w \dot X_{*,j})

  This can be calculated exactly, so this is essentially a noisy version of the exact calc...
  """
  N, R = X.shape
  t = np.zeros(N)
  f = np.zeros(N)

  # Take samples of random variables
  idxs = np.round(np.random.rand(n_samples) * (N-1)).astype(int)
  ct = np.bincount(idxs)
  # Estimate probability of correct assignment
  increment = np.random.rand(n_samples) < odds_to_prob(X[idxs, :].dot(w))
  increment_f = -1. * (increment - 1)
  t[idxs] = increment * ct[idxs]
  f[idxs] = increment_f * ct[idxs]
  
  return t, f

def exact_data(X, w, evidence=None):
  """
  We calculate the exact conditional probability of the decision variables in
  logistic regression; see sample_data
  """
  t = odds_to_prob(X.dot(w))
  if evidence is not None:
    t[evidence > 0.0] = 1.0
    t[evidence < 0.0] = 0.0
  return t, 1-t

def abs_sparse(X):
  """ Element-wise absolute value of sparse matrix """
  X_abs = X.copy()
  if sparse.isspmatrix_csr(X) or sparse.isspmatrix_csc(X):
    X_abs.data = np.abs(X_abs.data)
  elif sparse.isspmatrix_lil(X):
    X_abs.data = np.array([np.abs(L) for L in X_abs.data])
  else:
    raise ValueError("Only supports CSR/CSC and LIL matrices")
  return X_abs

def transform_sample_stats(Xt, t, f, Xt_abs=None):
  """
  Here we calculate the expected accuracy of each LF/feature
  (corresponding to the rows of X) wrt to the distribution of samples S:

    E_S[ accuracy_i ] = E_(t,f)[ \frac{TP + TN}{TP + FP + TN + FN} ]
                      = \frac{X_{i|x_{ij}>0}*t - X_{i|x_{ij}<0}*f}{t+f}
                      = \frac12\left(\frac{X*(t-f)}{t+f} + 1\right)
  """
  if Xt_abs is None:
    Xt_abs = abs_sparse(Xt) if sparse.issparse(Xt) else abs(Xt)
  n_pred = Xt_abs.dot(t+f)
  m = (1. / (n_pred + 1e-8)) * (Xt.dot(t) - Xt.dot(f))
  p_correct = (m + 1) / 2
  return p_correct, n_pred


class NoiseAwareModel(object):
    """Simple abstract base class for a model."""
    def __init__(self):
        pass

    def train(self, X, training_marginals=None, **hyperparams):
        raise NotImplementedError()

    def marginals(self, X):
        raise NotImplementedError()

    def predict(self, X):
        """Return numpy array of elements in {-1,0,1} based on predicted marginal probabilities."""
        return np.array([1 if p > 0.5 else -1 if p < 0.5 else 0 for p in self.marginals(X)])


class LogReg(NoiseAwareModel):
    """Logistic regression."""
    def __init__(self):
        self.w = None

    def train(self, X, training_marginals=None, n_iter=500, w0=None, rate=DEFAULT_RATE, alpha=DEFAULT_ALPHA, \
            mu=DEFAULT_MU, sample=False, n_samples=100, unreg=[], evidence=None, warm_starts=False, tol=1e-6, \
            verbose=True):
        """
        Perform SGD wrt the weights w
        * n_iter:      Number of steps of SGD
        * w0:          Initial value for weights w
        * rate:
        * alpha:       Elastic net penalty mixing parameter (0=ridge, 1=lasso)
        * mu:          Elastic net penalty
        * sample:      Whether to sample or not
        * n_samples:   Number of samples per SGD step
        * unreg:
        * evidence:    Ground truth to condition on
        * warm_starts:
        * tol:
        """
        N, M = X.shape
        Xt = X.transpose()
        Xt_abs = abs_sparse(Xt) if sparse.issparse(Xt) else np.abs(Xt)  
        w0 = w0 if w0 is not None else np.zeros(R)
        if training_marginals is not None:
            t,f = training_marginals, 1-training_marginals

        # Initialize training
        w = w0.copy()
        g = np.zeros(M)
        l = np.zeros(M)
        g_size = 0
        
        # Gradient descent
        if verbose:
            print "Begin training for rate={}, mu={}".format(rate, mu)
        for step in range(n_iter):
        
            # Get the expected LF accuracy
            if training_marginals is None:
                t,f = sample_data(X, w, n_samples=n_samples) if sample else exact_data(X, w, evidence)
            p_correct, n_pred = transform_sample_stats(Xt, t, f, Xt_abs)

            # Get the "empirical log odds"; NB: this assumes one is correct, clamp is for sampling...
            l = np.clip(log_odds(p_correct), -10, 10)
            
            # SGD step with normalization by the number of samples
            g0 = (n_pred*(w - l)) / np.sum(n_pred)
            
            # Momentum term for faster training
            g = 0.95*g0 + 0.05*g
            
            # Check for convergence
            wn = np.linalg.norm(w, ord=2)
            g_size = np.linalg.norm(g, ord=2)
            if step % 250 == 0 and verbose:    
                print "\tLearning epoch = {}\tGradient mag. = {:.6f}".format(step,g_size) 
            if (wn < 1e-12 or g_size / wn < tol) and step >= 10:
                if verbose:
                    print "SGD converged for mu={} after {} steps".format(mu, step)
                break
            
            # Update weights
            w -= rate * g
            
            # Store weights to not be regularized      
            w_unreg = w[unreg].copy()

            # Apply elastic net penalty
            soft = np.abs(w) - rate * alpha * mu
            ridge_pen = (1 + (1-alpha) * rate * mu)
            
            #          \ell_1 penalty by soft thresholding        |  \ell_2 penalty
            w = (np.sign(w)*np.select([soft>0], [soft], default=0)) / ridge_pen
            
            # Unregularize
            w[unreg] = w_unreg    
            
            # SGD did not converge    
        else:
            if verbose:
                print "Final gradient magnitude for rate={}, mu={}: {:.3f}".format(rate, mu, g_size)
        
        # Return learned weights  
        self.w = w

    def marginals(self, X):
        return odds_to_prob(X.dot(self.w))
        
      
def get_mu_seq(n, rate, alpha, min_ratio):
    mv = (max(float(1 + rate * 10), float(rate * 11)) / (alpha + 1e-3))
    return np.logspace(np.log10(mv * min_ratio), np.log10(mv), n)
