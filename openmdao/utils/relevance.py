"""
Class definitions for Relevance and related classes.
"""

from contextlib import contextmanager
from collections import defaultdict

import numpy as np

from openmdao.utils.general_utils import all_ancestors, _contains_all, get_rev_conns
from openmdao.utils.graph_utils import get_sccs_topo
from openmdao.utils.array_utils import array_hash
from openmdao.utils.om_warnings import issue_warning


def get_relevance(model, of, wrt):
    """
    Return a Relevance object for the given design vars, and responses.

    Parameters
    ----------
    model : <Group>
        The top level group in the system hierarchy.
    of : dict
        Dictionary of 'of' variables.  Keys don't matter.
    wrt : dict
        Dictionary of 'wrt' variables.  Keys don't matter.

    Returns
    -------
    Relevance
        Relevance object.
    """
    if not model._use_derivatives or (not of and not wrt):
        # in this case, a permanently inactive relevance object is returned
        # (so the contents of 'of' and 'wrt' don't matter). Make them empty to avoid
        # unnecessary setup.
        of = {}
        wrt = {}

    return Relevance(model, wrt, of)


class Relevance(object):
    """
    Class that computes relevance based on a data flow graph.

    Parameters
    ----------
    model : <Group>
        The top level group in the system hierarchy.
    fwd_meta : dict
        Dictionary of design variable metadata.  Keys don't matter.
    rev_meta : dict
        Dictionary of response variable metadata.  Keys don't matter.

    Attributes
    ----------
    _graph : <nx.DirectedGraph>
        Dependency graph.  Dataflow graph containing both variables and systems.
    _var2idx : dict
        dict of all variables in the graph mapped to the row index into the variable
        relevance array.
    _sys2idx : dict
        dict of all systems in the graph mapped to the row index into the system
        relevance array.
    _seed_vars : dict
        Maps direction to currently active seed variable names.
    _all_seed_vars : dict
        Maps direction to all seed variable names.
    _active : bool or None
        If True, relevance is active.  If False, relevance is inactive.  If None, relevance is
        uninitialized.
    _seed_var_map : dict
        Nested dict of the form {fwdseed(s): {revseed(s): var_array, ...}}.
        Keys that contain multiple seeds are frozensets of seed names.
    _seed_sys_map : dict
        Nested dict of the form {fwdseed(s): {revseed(s): sys_array, ...}}.
        Keys that contain multiple seeds are frozensets of seed names.
    _single_seed2relvars : dict
        Dict of the form {'fwd': {seed: var_array}, 'rev': ...} where each seed is a
        key and var_array is the variable relevance array for the given seed.
    _single_seed2relsys : dict
        Dict of the form {'fwd': {seed: sys_array}, 'rev': ...} where each seed is a
        key and var_array is the system relevance array for the given seed.
    _nonlinear_sets : dict
        Dict of the form {'pre': pre_rel_array, 'iter': iter_rel_array, 'post': post_rel_array}.
    _current_rel_varray : ndarray
        Array representing the combined variable relevance arrays for the currently active seeds.
    _current_rel_sarray : ndarray
        Array representing the combined system relevance arrays for the currently active seeds.
    _rel_array_cache : dict
        Cache of relevance arrays stored by array hash.
    _no_dv_responses : list
        List of responses that have no relevant design variables.
    """

    def __init__(self, model, fwd_meta, rev_meta):
        """
        Initialize all attributes.
        """
        assert model.pathname == '', "Relevance can only be initialized on the top level Group."

        self._active = None  # allow relevance to be turned on later
        self._graph = model._dataflow_graph
        self._rel_array_cache = {}
        self._no_dv_responses = []

        # seed var(s) for the current derivative operation
        self._seed_vars = {'fwd': frozenset(), 'rev': frozenset()}
        # all seed vars for the entire derivative computation
        self._all_seed_vars = {'fwd': frozenset(), 'rev': frozenset()}

        self._set_all_seeds(model, fwd_meta, rev_meta)

        self._current_rel_varray = None
        self._current_rel_sarray = None

        self._setup_nonlinear_relevance(model, fwd_meta, rev_meta)

        if model._pre_components or model._post_components:
            self._setup_nonlinear_sets(model)
        else:
            self._nonlinear_sets = {}

        if not (fwd_meta and rev_meta):
            self._active = False  # relevance will never be active

    def __repr__(self):
        """
        Return a string representation of the Relevance.

        Returns
        -------
        str
            String representation of the Relevance.
        """
        return f"Relevance({self._seed_vars}, active={self._active})"

    def _setup_nonlinear_sets(self, model):
        """
        Set up the nonlinear sets for relevance checking.

        Parameters
        ----------
        model : <Group>
            The top level group in the system hierarchy.
        """
        pre_systems = set()
        for compname in model._pre_components:
            pre_systems.update(all_ancestors(compname))
        if pre_systems:
            pre_systems.add('')  # include top level group

        post_systems = set()
        for compname in model._post_components:
            post_systems.update(all_ancestors(compname))
        if post_systems:
            post_systems.add('')

        pre_array = self._names2rel_array(pre_systems, self._all_systems, self._sys2idx)
        post_array = self._names2rel_array(post_systems, self._all_systems, self._sys2idx)

        if model._iterated_components is _contains_all:
            iter_array = np.ones(len(self._all_systems), dtype=bool)
        else:
            # iter_systems = set()
            # for compname in model._iterated_components:
            #     iter_systems.update(all_ancestors(compname))
            # if iter_systems:
            #     iter_systems.add('')

            # iter_array = self._names2rel_array(iter_systems, self._all_systems, self._sys2idx)
            iter_array = ~(pre_array | post_array)

        self._nonlinear_sets = {'pre': pre_array, 'iter': iter_array, 'post': post_array}

    def _single_seed_array_iter(self, group, seed_meta, direction, all_systems, all_vars):
        """
        Yield the relevance arrays for each individual seed for variables and systems.

        The relevance arrays are boolean ndarrays of length nvars and nsystems, respectively.
        All of the variables and systems in the graph map to an index into these arrays and
        if the value at that index is True, then the variable or system is relevant to the seed.

        Parameters
        ----------
        group : <Group>
            The top level group in the system hierarchy.
        seed_meta : dict
            Dictionary of metadata for the seeds.
        direction : str
            Direction of the search for relevant variables.  'fwd' or 'rev'.
        all_systems : set
            Set of all systems in the graph.
        all_vars : set
            Set of all variables in the graph.

        Yields
        ------
        str
            Name of the seed variable.
        bool
            True if the seed uses parallel derivative coloring.
        ndarray
            Boolean relevance array for the variables.
        ndarray
            Boolean relevance array for the systems.
        """
        nprocs = group.comm.size
        has_par_derivs = False

        for meta in seed_meta.values():
            src = meta['source']
            local = nprocs > 1 and meta['parallel_deriv_color'] is not None
            has_par_derivs |= local
            depnodes = self._dependent_nodes(src, direction, local=local)

            rel_systems = _vars2systems(depnodes)
            rel_vars = depnodes - all_systems

            yield (src, local, self._names2rel_array(rel_vars, all_vars, self._var2idx),
                   self._names2rel_array(rel_systems, all_systems, self._sys2idx))

    def _names2rel_array(self, names, all_names, names2inds):
        """
        Return a relevance array for the given names.

        Parameters
        ----------
        names : iter of str
            Iterator over names.
        all_names : iter of str
            Iterator over the full set of names from the graph, either variables or systems.
        names2inds : dict
            Dict of the form {name: index} where index is the index into the relevance array.

        Returns
        -------
        ndarray
            Boolean relevance array.  True means name is relevant.
        """
        rel_array = np.zeros(len(all_names), dtype=bool)
        rel_array[[names2inds[n] for n in names]] = True

        return self._get_cached_array(rel_array)

    def _get_cached_array(self, arr):
        """
        Return the cached array if it exists, otherwise return the input array after caching it.

        Parameters
        ----------
        arr : ndarray
            Array to be cached.

        Returns
        -------
        ndarray
            Cached array if it exists, otherwise the input array.
        """
        hash = array_hash(arr)
        if hash in self._rel_array_cache:
            return self._rel_array_cache[hash]
        else:
            self._rel_array_cache[hash] = arr

        return arr

    def _combine_relevance(self, fmap, fwd_seeds, rmap, rev_seeds):
        """
        Return the combined relevance arrays for the given seeds.

        Parameters
        ----------
        fmap : dict
            Dict of the form {seed: array} where array is the
            relevance arrays for the given seed.
        fwd_seeds : iter of str
            Iterator over forward seed variable names.
        rmap : dict
            Dict of the form {seed: array} where array is the
            relevance arrays for the given seed.
        rev_seeds : iter of str
            Iterator over reverse seed variable names.

        Returns
        -------
        ndarray
            Array representing the combined relevance arrays for the given seeds.
            The arrays are combined by taking the union of the fwd seeds and the union of the
            rev seeds and intersecting the two results.
        """
        # get the union of the fwd relevance and the union of the rev relevance
        farray = self._union_arrays(fmap, fwd_seeds)
        rarray = self._union_arrays(rmap, rev_seeds)

        # intersect the two results
        farray &= rarray

        return self._get_cached_array(farray)

    def _union_arrays(self, seed_map, seeds):
        """
        Return the intersection of the relevance arrays for the given seeds.

        Parameters
        ----------
        seed_map : dict
            Dict of the form {seed: rel_array} where rel_array is the relevance array for the
            given seed.
        seeds : iter of str
            Iterator over forward seed variable names.

        Returns
        -------
        ndarray
            The array representing the intersection of the relevance arrays for the given seeds.
        """
        if not seeds:
            return np.zeros(0, dtype=bool)

        for i, seed in enumerate(seeds):
            arr = seed_map[seed]
            if i == 0:
                array = arr.copy()
            else:
                array |= arr

        return array

    def _rel_names_iter(self, rel_array, all_names, relevant=True):
        """
        Return an iterator of names from the given relevance array.

        Parameters
        ----------
        rel_array : ndarray
            Boolean relevance array.  True means name is relevant.
        all_names : iter of str
            Iterator over the full set of names from the graph, either variables or systems.
        relevant : bool
            If True, return only relevant names.  If False, return only irrelevant names.

        Yields
        ------
        str
            Name from the given relevance array.
        """
        for n, rel in zip(all_names, rel_array):
            if rel == relevant:
                yield n

    def _set_all_seeds(self, group, fwd_meta, rev_meta):
        """
        Set the full list of seeds to be used to determine relevance.

        This should only be called once, at __init__ time.

        Parameters
        ----------
        group : <Group>
            The top level group in the system hierarchy.
        fwd_meta : dict
            Dictionary of metadata for forward derivatives.
        rev_meta : dict
            Dictionary of metadata for reverse derivatives.
        """
        fwd_seeds = frozenset([m['source'] for m in fwd_meta.values()])
        rev_seeds = frozenset([m['source'] for m in rev_meta.values()])

        self._seed_var_map = seed_var_map = {}
        self._seed_sys_map = seed_sys_map = {}

        self._current_var_array = np.zeros(0, dtype=bool)
        self._current_sys_array = np.zeros(0, dtype=bool)

        self._all_seed_vars['fwd'] = fwd_seeds
        self._all_seed_vars['rev'] = rev_seeds

        self._single_seed2relvars = {'fwd': {}, 'rev': {}}
        self._single_seed2relsys = {'fwd': {}, 'rev': {}}

        if not fwd_meta or not rev_meta:
            return

        # this set contains all variables and some or all components
        # in the graph.  Components are included if all of their outputs
        # depend on all of their inputs.
        all_vars = set()
        all_systems = {''}
        for node, data in self._graph.nodes(data=True):
            if 'type_' in data:
                all_vars.add(node)
                sysname = node.rpartition('.')[0]
                if sysname not in all_systems:
                    all_systems.update(all_ancestors(sysname))
            elif node not in all_systems:
                all_systems.update(all_ancestors(node))

        self._all_systems = all_systems

        # create mappings of var and system names to indices into the var/system
        # relevance arrays.
        self._sys2idx = {n: i for i, n in enumerate(all_systems)}
        self._var2idx = {n: i for i, n in enumerate(all_vars)}

        meta = {'fwd': fwd_meta, 'rev': rev_meta}

        # map each seed to its variable and system relevance arrays
        has_par_derivs = {}
        for io in ('fwd', 'rev'):
            for seed, local, var_array, sys_array in self._single_seed_array_iter(group, meta[io],
                                                                                  io, all_systems,
                                                                                  all_vars):
                self._single_seed2relvars[io][seed] = self._get_cached_array(var_array)
                self._single_seed2relsys[io][seed] = self._get_cached_array(sys_array)
                if local:
                    has_par_derivs[seed] = io

        # in seed_map, add keys for both fsrc and frozenset((fsrc,)) and similarly for rsrc
        # because both forms of keys may be used depending on the context.
        for fseed, fvarr in self._single_seed2relvars['fwd'].items():
            fsarr = self._single_seed2relsys['fwd'][fseed]
            seed_var_map[fseed] = seed_var_map[frozenset((fseed,))] = vsub = {}
            seed_sys_map[fseed] = seed_sys_map[frozenset((fseed,))] = ssub = {}
            for rsrc, rvarr in self._single_seed2relvars['rev'].items():
                rsysarr = self._single_seed2relsys['rev'][rsrc]
                vsub[rsrc] = vsub[frozenset((rsrc,))] = self._get_cached_array(fvarr & rvarr)
                ssub[rsrc] = ssub[frozenset((rsrc,))] = self._get_cached_array(fsarr & rsysarr)

        all_fseed_varray = self._union_arrays(self._single_seed2relvars['fwd'], fwd_seeds)
        all_fseed_sarray = self._union_arrays(self._single_seed2relsys['fwd'], fwd_seeds)

        all_rseed_varray = self._union_arrays(self._single_seed2relvars['rev'], rev_seeds)
        all_rseed_sarray = self._union_arrays(self._single_seed2relsys['rev'], rev_seeds)

        # now add entries for each (fseed, all_rseeds) and each (all_fseeds, rseed)
        for fsrc, farr in self._single_seed2relvars['fwd'].items():
            fsysarr = self._single_seed2relsys['fwd'][fsrc]
            seed_var_map[fsrc][rev_seeds] = self._get_cached_array(farr & all_rseed_varray)
            seed_sys_map[fsrc][rev_seeds] = self._get_cached_array(fsysarr & all_rseed_sarray)

        seed_var_map[fwd_seeds] = {}
        seed_sys_map[fwd_seeds] = {}
        for rsrc, rarr in self._single_seed2relvars['rev'].items():
            rsysarr = self._single_seed2relsys['rev'][rsrc]
            seed_var_map[fwd_seeds][rsrc] = self._get_cached_array(rarr & all_fseed_varray)
            seed_sys_map[fwd_seeds][rsrc] = self._get_cached_array(rsysarr & all_fseed_sarray)

        # now add 'full' releveance for all seeds
        seed_var_map[fwd_seeds][rev_seeds] = self._get_cached_array(all_fseed_varray &
                                                                    all_rseed_varray)
        seed_sys_map[fwd_seeds][rev_seeds] = self._get_cached_array(all_fseed_sarray &
                                                                    all_rseed_sarray)

        self._set_seeds(fwd_seeds, rev_seeds)

        if has_par_derivs:
            self._par_deriv_err_check(group, rev_meta, fwd_meta)

        farrs = {}
        rarrs = {}
        for fsrc, farr in self._single_seed2relvars['fwd'].items():
            if fsrc in has_par_derivs:
                direction = has_par_derivs[fsrc]
                depnodes = self._dependent_nodes(fsrc, direction, local=False)
                rel_vars = depnodes - all_systems
                farr = self._names2rel_array(rel_vars, all_vars, self._var2idx)
            farrs[fsrc] = farr

        for rsrc, rarr in self._single_seed2relvars['rev'].items():
            if rsrc in has_par_derivs:
                direction = has_par_derivs[rsrc]
                depnodes = self._dependent_nodes(rsrc, direction, local=False)
                rel_vars = depnodes - all_systems
                rarr = self._names2rel_array(rel_vars, all_vars, self._var2idx)
            rarrs[rsrc] = rarr

        allfarrs = self._union_arrays(farrs, fwd_seeds)
        self._no_dv_responses = []
        for rsrc, rarr in rarrs.items():
            if not (allfarrs & rarr)[self._var2idx[rsrc]]:
                self._no_dv_responses.append(rsrc)

    @contextmanager
    def active(self, active):
        """
        Context manager for activating/deactivating relevance.

        Parameters
        ----------
        active : bool
            If True, activate relevance.  If False, deactivate relevance.

        Yields
        ------
        None
        """
        if not self._active:  # if already inactive from higher level, don't change it
            yield
        else:
            save = self._active
            self._active = active
            try:
                yield
            finally:
                self._active = save

    def relevant_vars(self, name, direction, inputs=True, outputs=True):
        """
        Return a set of variables relevant to the given dv/response in the given direction.

        Parameters
        ----------
        name : str
            Name of the variable of interest.
        direction : str
            Direction of the search for relevant variables.  'fwd' or 'rev'.
        inputs : bool
            If True, include inputs.
        outputs : bool
            If True, include outputs.

        Returns
        -------
        set
            Set of the relevant variables.
        """
        names = self._rel_names_iter(self._single_seed2relvars[direction][name], self._var2idx)
        if inputs and outputs:
            return set(names)
        elif inputs:
            return self._apply_node_filter(names, _is_input)
        elif outputs:
            return self._apply_node_filter(names, _is_output)
        else:
            return set()

    @contextmanager
    def all_seeds_active(self):
        """
        Context manager where all seeds are active.

        This assumes that the relevance object itself is active.

        Yields
        ------
        None
        """
        # if already inactive from higher level, or 'active' parameter is False, don't change it
        if self._active is False:
            yield
        else:
            save = {'fwd': self._seed_vars['fwd'], 'rev': self._seed_vars['rev']}
            save_active = self._active
            self._active = True
            self._set_seeds(self._all_seed_vars['fwd'], self._all_seed_vars['rev'])
            try:
                yield
            finally:
                self._seed_vars = save
                self._active = save_active

    @contextmanager
    def seeds_active(self, fwd_seeds=None, rev_seeds=None):
        """
        Context manager where the specified seeds are active.

        This assumes that the relevance object itself is active.

        Parameters
        ----------
        fwd_seeds : iter of str or None
            Iterator over forward seed variable names. If None use current active seeds.
        rev_seeds : iter of str or None
            Iterator over reverse seed variable names. If None use current active seeds.

        Yields
        ------
        None
        """
        if self._active is False:  # if already inactive from higher level, don't change anything
            yield
        else:
            save = {'fwd': self._seed_vars['fwd'], 'rev': self._seed_vars['rev']}
            save_active = self._active
            self._active = True
            fwd_seeds = self._all_seed_vars['fwd'] if fwd_seeds is None else frozenset(fwd_seeds)
            rev_seeds = self._all_seed_vars['rev'] if rev_seeds is None else frozenset(rev_seeds)
            self._set_seeds(fwd_seeds, rev_seeds)
            try:
                yield
            finally:
                self._seed_vars = save
                self._active = save_active

    @contextmanager
    def activate_nonlinear(self, name, active=True):
        """
        Context manager for activating a subset of systems using 'pre' or 'post'.

        Parameters
        ----------
        name : str
            Name of the set to activate.
        active : bool
            If False, relevance is temporarily deactivated.

        Yields
        ------
        None
        """
        if not active or self._active is False or name not in self._nonlinear_sets:
            yield
        else:
            save_active = self._active
            self._active = True
            self._current_rel_sarray = self._nonlinear_sets[name]

            try:
                yield
            finally:
                self._active = save_active

    def _set_seeds(self, fwd_seeds, rev_seeds):
        """
        Set the seed(s) to determine relevance for a given variable in a given direction.

        Parameters
        ----------
        fwd_seeds : frozenset
            Set of forward seed variable names.
        rev_seeds : frozenset
            Set of reverse seed variable names.
        """
        self._seed_vars['fwd'] = fwd_seeds
        self._seed_vars['rev'] = rev_seeds

        if fwd_seeds and rev_seeds:
            self._current_rel_varray = self._get_rel_array(self._seed_var_map,
                                                           self._single_seed2relvars,
                                                           fwd_seeds, rev_seeds)
            self._current_rel_sarray = self._get_rel_array(self._seed_sys_map,
                                                           self._single_seed2relsys,
                                                           fwd_seeds, rev_seeds)

    def _get_rel_array(self, seed_map, single_seed2rel, fwd_seeds, rev_seeds):
        """
        Return the combined relevance array for the given seeds.

        If it doesn't exist, create it.

        Parameters
        ----------
        seed_map : dict
            Dict of the form {fwdseed: {revseed: rel_arrays}}.
        single_seed2rel : dict
            Dict of the form {'fwd': {seed: rel_array}, 'rev': ...} where each seed is a key and
            rel_array is the relevance array for the given seed.
        fwd_seeds : str or frozenset of str
            Iterator over forward seed variable names.
        rev_seeds : str or frozenset of str
            Iterator over reverse seed variable names.

        Returns
        -------
        ndarray
            Array representing the combined relevance arrays for the given seeds.
        """
        try:
            return seed_map[fwd_seeds][rev_seeds]
        except KeyError:
            # print(f"missing rel array for ({fwd_seeds}, {rev_seeds})")
            relarr = self._combine_relevance(single_seed2rel['fwd'], fwd_seeds,
                                             single_seed2rel['rev'], rev_seeds)
            if fwd_seeds not in seed_map:
                seed_map[fwd_seeds] = {}
            seed_map[fwd_seeds][rev_seeds] = relarr

        return relarr

    def is_relevant(self, name):
        """
        Return True if the given variable is relevant.

        Parameters
        ----------
        name : str
            Name of the variable.

        Returns
        -------
        bool
            True if the given variable is relevant.
        """
        if not self._active:
            return True

        return self._current_rel_varray[self._var2idx[name]]

    def is_relevant_system(self, name):
        """
        Return True if the given named system is relevant.

        Parameters
        ----------
        name : str
            Name of the System.

        Returns
        -------
        bool
            True if the given system is relevant.
        """
        if not self._active:
            return True

        return self._current_rel_sarray[self._sys2idx[name]]

    def filter(self, systems, relevant=True):
        """
        Filter the given iterator of systems to only include those that are relevant.

        Parameters
        ----------
        systems : iter of Systems
            Iterator over systems.
        relevant : bool
            If True, return only relevant systems.  If False, return only irrelevant systems.

        Yields
        ------
        System
            Relevant system.
        """
        if self._active:
            for system in systems:
                if relevant == self.is_relevant_system(system.pathname):
                    yield system
        elif relevant:
            yield from systems

    def iter_seed_pair_relevance(self, fwd_seeds=None, rev_seeds=None, inputs=False, outputs=False):
        """
        Yield all relevant variables for each pair of seeds.

        Parameters
        ----------
        fwd_seeds : iter of str or None
            Iterator over forward seed variable names. If None use current registered seeds.
        rev_seeds : iter of str or None
            Iterator over reverse seed variable names. If None use current registered seeds.
        inputs : bool
            If True, include inputs.
        outputs : bool
            If True, include outputs.

        Yields
        ------
        set
            Set of names of relevant variables.
        """
        filt = _get_io_filter(inputs, outputs)
        if filt is True:  # everything is filtered out
            return

        if fwd_seeds is None:
            fwd_seeds = self._seed_vars['fwd']
        if rev_seeds is None:
            rev_seeds = self._seed_vars['rev']

        if isinstance(fwd_seeds, str):
            fwd_seeds = [fwd_seeds]
        if isinstance(rev_seeds, str):
            rev_seeds = [rev_seeds]

        for seed in fwd_seeds:
            for rseed in rev_seeds:
                inter = self._get_rel_array(self._seed_var_map, self._single_seed2relvars,
                                            seed, rseed)
                if np.any(inter):
                    inter = self._rel_names_iter(inter, self._var2idx)
                    yield seed, rseed, self._apply_node_filter(inter, filt)

    def _apply_node_filter(self, names, filt):
        """
        Return only the nodes from the given set of nodes that pass the given filter.

        Parameters
        ----------
        names : iter of str
            Iterator of node names.
        filt : callable
            Filter function taking a graph node as an argument and returning True if the node
            should be included in the output.  If True, no filtering is done.  If False, the
            returned set will be empty.

        Returns
        -------
        set
            Set of node names that passed the filter.
        """
        if not filt:  # no filtering needed
            if isinstance(names, set):
                return names
            return set(names)
        elif filt is True:
            return set()

        # filt is a function.  Apply it to named graph nodes.
        return set(self._filter_nodes_iter(names, filt))

    def _filter_nodes_iter(self, names, filt):
        """
        Return only the nodes from the given set of nodes that pass the given filter.

        Parameters
        ----------
        names : iter of str
            Iterator over node names.
        filt : callable
            Filter function taking a graph node as an argument and returning True if the node
            should be included in the output.

        Yields
        ------
        str
            Node name that passed the filter.
        """
        nodes = self._graph.nodes
        for n in names:
            if filt(nodes[n]):
                yield n

    def _all_relevant(self, fwd_seeds, rev_seeds, inputs=True, outputs=True):
        """
        Return all relevant inputs, outputs, and systems for the given seeds.

        This is primarily used as a convenience function for testing and is not particularly
        efficient.

        Parameters
        ----------
        fwd_seeds : iter of str
            Iterator over forward seed variable names.
        rev_seeds : iter of str
            Iterator over reverse seed variable names.
        inputs : bool
            If True, include inputs.
        outputs : bool
            If True, include outputs.

        Returns
        -------
        tuple
            (set of relevant inputs, set of relevant outputs, set of relevant systems)
            If a given inputs/outputs is False, the corresponding set will be empty. The
            returned systems will be the set of all systems containing any
            relevant variables based on the values of inputs and outputs, i.e. if outputs is False,
            the returned systems will be the set of all systems containing any relevant inputs.
        """
        relevant_vars = set()
        for _, _, relvars in self.iter_seed_pair_relevance(fwd_seeds, rev_seeds, inputs, outputs):
            relevant_vars.update(relvars)
        relevant_systems = _vars2systems(relevant_vars)

        inputs = set(self._filter_nodes_iter(relevant_vars, _is_input))
        outputs = set(self._filter_nodes_iter(relevant_vars, _is_output))

        return inputs, outputs, relevant_systems

    def _dependent_nodes(self, start, direction, local=False):
        """
        Return set of all connected nodes in the given direction starting at the given node.

        Parameters
        ----------
        start : str
            Name of the starting node.
        direction : str
            If 'fwd', traverse downstream.  If 'rev', traverse upstream.
        local : bool
            If True, include only local variables.

        Returns
        -------
        set
            Set of all dependent nodes.
        """
        if start in self._graph:
            if local and not self._graph.nodes[start]['local']:
                return set()

            if direction == 'fwd':
                fnext = self._graph.successors
            elif direction == 'rev':
                fnext = self._graph.predecessors
            else:
                raise ValueError("direction must be 'fwd' or 'rev'")

            stack = [start]
            visited = {start}

            while stack:
                src = stack.pop()
                for tgt in fnext(src):
                    if tgt not in visited:
                        if local:
                            node = self._graph.nodes[tgt]
                            # stop local traversal at the first non-local node
                            if 'local' in node and not node['local']:
                                return visited

                        visited.add(tgt)
                        stack.append(tgt)

            return visited

        return set()

    def _par_deriv_err_check(self, group, responses, desvars):
        pd_err_chk = defaultdict(dict)
        mode = group._problem_meta['mode']  # 'fwd', 'rev', or 'auto'

        if mode in ('fwd', 'auto'):
            for desvar, response, relset in self.iter_seed_pair_relevance(inputs=True):
                if desvar in desvars and self._graph.nodes[desvar]['local']:
                    dvcolor = desvars[desvar]['parallel_deriv_color']
                    if dvcolor:
                        pd_err_chk[dvcolor][desvar] = relset

        if mode in ('rev', 'auto'):
            for desvar, response, relset in self.iter_seed_pair_relevance(outputs=True):
                if response in responses and self._graph.nodes[response]['local']:
                    rescolor = responses[response]['parallel_deriv_color']
                    if rescolor:
                        pd_err_chk[rescolor][response] = relset

        # check to make sure we don't have any overlapping dependencies between vars of the
        # same color
        errs = {}
        for pdcolor, dct in pd_err_chk.items():
            for vname, relset in dct.items():
                for n, nds in dct.items():
                    if vname != n and relset.intersection(nds):
                        if pdcolor not in errs:
                            errs[pdcolor] = []
                        errs[pdcolor].append(vname)

        all_errs = group.comm.allgather(errs)
        msg = []
        for errdct in all_errs:
            for color, names in errdct.items():
                vtype = 'design variable' if mode == 'fwd' else 'response'
                msg.append(f"Parallel derivative color '{color}' has {vtype}s "
                           f"{sorted(names)} with overlapping dependencies on the same rank.")

        if msg:
            raise RuntimeError('\n'.join(msg))

    def _setup_nonlinear_relevance(self, model, designvars, responses):
        """
        Set up the iteration lists containing the pre, iterated, and post subsets of systems.

        This should only be called on the top level Group.

        Parameters
        ----------
        model : <Group>
            The top level group in the system hierarchy.
        designvars : dict
            A dict of all design variables from the model.
        responses : dict
            A dict of all responses from the model.
        """
        # don't redo this if it's already been done
        if model._pre_components is not None:
            return

        model._pre_components = set()
        model._post_components = set()
        model._iterated_components = _contains_all

        if not designvars or not responses or not model._problem_meta['group_by_pre_opt_post']:
            return

        # keep track of Groups with nonlinear solvers that use gradients (like Newton) and certain
        # linear solvers like DirectSolver. These groups and all systems they contain must be
        # grouped together into the same iteration list.
        grad_groups = set()
        always_opt = set()
        model._get_relevance_modifiers(grad_groups, always_opt)

        if '' in grad_groups:
            issue_warning("The top level group has a nonlinear solver that computes gradients, so "
                          "the entire model will be included in the optimization iteration.")
            return

        dvs = [meta['source'] for meta in designvars.values()]
        responses = [meta['source'] for meta in responses.values()]
        responses = set(responses)  # get rid of dups due to aliases

        graph = model.compute_sys_graph(comps_only=True, add_edge_info=False)

        auto_dvs = [dv for dv in dvs if dv.startswith('_auto_ivc.')]
        dv0 = auto_dvs[0] if auto_dvs else dvs[0].rpartition('.')[0]

        if auto_dvs:
            rev_conns = get_rev_conns(model._conn_global_abs_in2out)

            # add nodes for any auto_ivc vars that are dvs and connect to downstream component(s)
            for dv in auto_dvs:
                graph.add_node(dv, type_='output')
                inps = rev_conns.get(dv, ())
                for inp in inps:
                    inpcomp = inp.rpartition('.')[0]
                    graph.add_edge(dv, inpcomp)

        # One way to determine the contents of the pre/opt/post sets is to add edges from the
        # response variables to the design variables and vice versa, then find the strongly
        # connected components of the resulting graph.  get_sccs_topo returns the strongly
        # connected components in topological order, so we can use it to give us pre, iterated,
        # and post subsets of the systems.

        # add edges between response comps and design vars/comps to form a strongly
        # connected component for all nodes involved in the optimization iteration.
        for res in responses:
            resnode = res.rpartition('.')[0]
            for dv in dvs:
                dvnode = dv.rpartition('.')[0]
                if dvnode == '_auto_ivc':
                    # var node exists in graph so connect it to resnode
                    dvnode = dv  # use var name not comp name

                graph.add_edge(resnode, dvnode)
                graph.add_edge(dvnode, resnode)

        # loop 'always_opt' components into all responses to force them to be relevant during
        # optimization.
        for opt_sys in always_opt:
            for response in responses:
                rescomp = response.rpartition('.')[0]
                graph.add_edge(opt_sys, rescomp)
                graph.add_edge(rescomp, opt_sys)

        groups_added = set()

        if grad_groups:
            remaining = set(grad_groups)
            for name in sorted(grad_groups, key=lambda x: x.count('.')):
                prefix = name + '.'
                match = {n for n in remaining if n.startswith(prefix)}
                remaining -= match

            gradlist = '\n'.join(sorted(remaining))
            issue_warning("The following groups have a nonlinear solver that computes gradients "
                          f"and will be treated as atomic for the purposes of determining "
                          f"which systems are included in the optimization iteration: "
                          f"\n{gradlist}\n")

            # remaining groups are not contained within a higher level nl solver
            # using gradient group, so make new connections to/from them to
            # all systems that they contain.  This will force them to be
            # treated as 'atomic' within the graph, so that if they contain
            # any dv or response systems, or if their children are connected to
            # both dv *and* response systems, then all systems within them will
            # be included in the 'opt' set.  Note that this step adds some group nodes
            # to the graph where before it only contained component nodes and auto_ivc
            # var nodes.
            edges_to_add = []
            for grp in remaining:
                prefix = grp + '.'
                for node in graph:
                    if node.startswith(prefix):
                        groups_added.add(grp)
                        edges_to_add.append((grp, node))
                        edges_to_add.append((node, grp))

            graph.add_edges_from(edges_to_add)

        # this gives us the strongly connected components in topological order
        sccs = get_sccs_topo(graph)

        pre = addto = set()
        post = set()
        iterated = set()
        for strong_con in sccs:
            # because the sccs are in topological order and all design vars and
            # responses are in the iteration set, we know that until we
            # see a design var or response, we're in the pre-opt set.  Once we
            # see a design var or response, we're in the iterated set.  Once
            # we see an scc without a design var or response, we're in the
            # post-opt set.
            if dv0 in strong_con:
                for s in strong_con:
                    if 'type_' in graph.nodes[s]:
                        s = s.rpartition('.')[0]
                    if s not in iterated:
                        iterated.add(s)
                addto = post
            else:
                for s in strong_con:
                    if 'type_' in graph.nodes[s]:
                        s = s.rpartition('.')[0]
                    if s not in addto:
                        addto.add(s)

        auto_ivc = model._auto_ivc
        auto_dvs = set(auto_dvs)
        rev_conns = get_rev_conns(model._conn_global_abs_in2out)
        if '_auto_ivc' not in pre:
            in_pre = False
            for vname in auto_ivc._var_abs2prom['output']:
                if vname not in auto_dvs:
                    for tgt in rev_conns[vname]:
                        tgtcomp = tgt.rpartition('.')[0]
                        if tgtcomp in pre:
                            in_pre = True
                            break
                    if in_pre:
                        break
            if in_pre:
                pre.add('_auto_ivc')

        # if 'pre' contains nothing but _auto_ivc, then just make it empty
        if len(pre) == 1 and '_auto_ivc' in pre:
            pre.discard('_auto_ivc')

        model._pre_components = pre - groups_added
        model._post_components = post - groups_added
        model._iterated_components = iterated - groups_added

    def list_relevance(self, relevant=True, type='system'):
        """
        Return a list of relevant variables and systems for the given seeds.

        Parameters
        ----------
        relevant : bool
            If True, return only relevant variables and systems.  If False, return only irrelevant
            variables and systems.
        type : str
            If 'system', return only system names.  If 'var', return only variable names.

        Returns
        -------
        list of str
            List of (ir)relevant variables or systems.
        """
        if type == 'system':
            it = self._rel_names_iter(self._current_rel_sarray, self._sys2idx, relevant)
        else:
            it = self._rel_names_iter(self._current_rel_varray, self._var2idx, relevant)

        return list(it)

def _vars2systems(nameiter):
    """
    Return a set of all systems containing the given variables or components.

    This includes all ancestors of each system, including ''.

    Parameters
    ----------
    nameiter : iter of str
        Iterator of variable or component pathnames.

    Returns
    -------
    set
        Set of system pathnames.
    """
    systems = {''}  # root group is always there
    for name in nameiter:
        sysname = name.rpartition('.')[0]
        if sysname not in systems:
            systems.update(all_ancestors(sysname))

    return systems


def _get_io_filter(inputs, outputs):
    if inputs and outputs:
        return False  # no filtering needed
    elif inputs:
        return _is_input
    elif outputs:
        return _is_output
    else:
        return True  # filter out everything


def _is_input(node):
    return node['type_'] == 'input'


def _is_output(node):
    return node['type_'] == 'output'
