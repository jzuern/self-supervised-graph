from builtins import Exception
import os
import networkx as nx
import wandb
import argparse
from tqdm import tqdm
import numpy as np
import time
import torch
import torch.utils.data
import torch_geometric.data
from torchmetrics.classification import Accuracy, Precision, Recall
import matplotlib.pyplot as plt

from torch_geometric.nn import DataParallel
from torch_geometric.loader import DataListLoader
from torch_geometric.data import Batch


from lanegnn.lanegnn import LaneGNN
from lanegnn.utils import ParamLib, assign_edge_lengths
from data.datasets import PreprocessedDataset
from metrics.metrics import calc_all_metrics



class Trainer():

    def __init__(self, params, model, dataloader_train, dataloader_val, optimizer):

        self.model = model
        self.dataloader_train = dataloader_train
        self.dataloader_val = dataloader_val
        self.params = params
        self.optimizer = optimizer
        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        self.total_step = 0

        self.figure, self.axarr = plt.subplots(1, 2)

        print(len(self.dataloader_train))

        it = iter(self.dataloader_train)
        i = 0
        while i < 1:
            i += 1
            self.one_sample_data = next(it)

    def crappy_edge_batch_detection(self, edge_indices):
        edge_indices = edge_indices.cpu().numpy()
        # detect jumps in edge_batch
        edge_batch_diff = np.diff(edge_indices[:, 0])
        jumps = np.where(edge_batch_diff < -10)[0]

        return jumps


    def do_logging(self, data, edge_scores_pred, node_scores_pred, plot_text):
        
        print("\nLogging synchronously...")
        figure_log, axarr_log = plt.subplots(1, 2, figsize=(15, 8))
        plt.tight_layout()

        if self.params.model.dataparallel:
            data = Batch.from_data_list(data)

        # Do logging
        if data.edge_indices.shape[0] == 2:
            data.edge_indices = data.edge_indices.t().contiguous()

        num_edges_in_batch = self.crappy_edge_batch_detection(data.edge_indices)
        if len(num_edges_in_batch) > 0:
            num_edges_in_batch = num_edges_in_batch[0]
        else:
            num_edges_in_batch = data.edge_indices.shape[0]

        node_pos = data.node_feats[data.batch == 0].cpu().numpy()
        node_scores_target = data.node_scores[data.batch == 0].cpu().numpy()
        edge_scores_target = data.edge_scores[:num_edges_in_batch].cpu().numpy()
        edge_indices = data.edge_indices[:num_edges_in_batch].cpu().numpy()

        node_scores_pred = node_scores_pred.cpu()
        edge_scores_pred = edge_scores_pred.cpu()
        data = data.cpu()


        # Calculate node and edge score accuracies
        acc_node = Accuracy(num_classes=1)(node_scores_pred, torch.round(data.node_scores).int())
        acc_edge = Accuracy(num_classes=1)(edge_scores_pred, torch.round(data.edge_scores).int())
        recall_node = Recall(task="binary")(torch.round(node_scores_pred), torch.round(data.node_scores).int())
        recall_edge = Recall(task="binary")(torch.round(edge_scores_pred), torch.round(data.edge_scores).int())
        precision_node = Precision(task="binary")(torch.round(node_scores_pred), torch.round(data.node_scores).int())
        precision_edge = Precision(task="binary")(torch.round(edge_scores_pred), torch.round(data.edge_scores).int())

        node_scores_pred = node_scores_pred[data.batch.cpu() == 0].detach().cpu().numpy()
        edge_scores_pred = edge_scores_pred[:num_edges_in_batch].detach().cpu().numpy()

        num_rgb_rows = data.rgb.shape[0] // np.unique(data.batch.cpu().numpy()).shape[0]
        rgb = data.rgb[:num_rgb_rows].cpu().numpy()

        graph_target = nx.DiGraph()
        graph_pred = nx.DiGraph()

        for i in range(node_pos.shape[0]):
            graph_target.add_node(i, pos=node_pos[i])
            graph_pred.add_node(i, pos=node_pos[i])

        for edge_idx, edge in enumerate(edge_indices):
            i, j = edge
            i, j = i.item(), j.item()
            if graph_target.has_node(i) and graph_target.has_node(j):
                graph_target.add_edge(i, j, weight=1-edge_scores_target[edge_idx])

        for edge_idx, edge in enumerate(edge_indices):
            i, j = edge
            i, j = i.item(), j.item()
            if graph_pred.has_node(i) and graph_pred.has_node(j):
                graph_pred.add_edge(i, j, weight=1-edge_scores_pred[edge_idx])

        cmap = plt.get_cmap('viridis')
        color_edge_target = cmap(edge_scores_target)[:, 0:4]
        color_node_target = cmap(node_scores_target)[:, 0:4]
        color_edge_target[:, -1] = edge_scores_target
        color_edge_pred = cmap(edge_scores_pred)[:, 0:4]
        color_node_pred = cmap(node_scores_pred)[:, 0:4]
        color_edge_pred[:, -1] = edge_scores_pred


        axarr_log[0].cla()
        axarr_log[1].cla()
        axarr_log[0].imshow(rgb)
        axarr_log[1].imshow(rgb)
        axarr_log[0].axis('off')
        axarr_log[1].axis('off')
        for i in range(len(axarr_log)):
            axarr_log[i].set_xlim([0, rgb.shape[1]])
            axarr_log[i].set_ylim([rgb.shape[0], 0])

        # Draw GT graph
        nx.draw_networkx(graph_target,
                         ax=axarr_log[0],
                         pos=node_pos,
                         edge_color=color_edge_target,
                         node_color=color_node_target,
                         with_labels=False,
                         width=2, arrowsize=4,
                         node_size=20)

        nx.draw_networkx(graph_pred,
                         ax=axarr_log[1],
                         pos=node_pos,
                         edge_color=color_edge_pred,
                         node_color=color_node_pred,
                         with_labels=False,
                         width=2, arrowsize=4,
                         node_size=20)

        # drawing updated values
        figure_log.canvas.draw()
        figure_log.canvas.flush_events()

        imname = "viz/{:05d}.png".format(self.total_step)
        try:
            plt.savefig(imname)
            print("Saved logging image to {}".format(imname))
        except Exception as e:
            pass

        if not self.params.main.disable_wandb:
            wandb.log({plot_text: figure_log})

        del figure_log
        del axarr_log
        plt.close()



    def train(self, epoch):

        self.model.train()
        epoch_start = 0

        train_progress = tqdm(self.dataloader_train)
        for step, data in enumerate(train_progress):

            if step == 1:
                epoch_start = time.time()

            t_start = time.time()
            self.optimizer.zero_grad()

            if self.params.model.dataparallel:
                data = [item.to(self.device) for item in data]
            else:
                data = data.to(self.device)

            # loss and optim
            edge_scores, node_scores, _ = self.model(data)
            edge_scores = torch.nn.Sigmoid()(edge_scores).squeeze()
            node_scores = torch.nn.Sigmoid()(node_scores).squeeze()

            # Convert list of Data to DataBatch for post-processing and loss calculation
            if self.params.model.dataparallel:
                data_orig = data.copy()
                data = Batch.from_data_list(data)

            # # loss and optim
            edge_weight = torch.ones_like(data.edge_scores)
            node_weight = torch.ones_like(data.node_scores)

            # Specify ignore regions
            # if self.params.model.ignore_low_scores:
            #     edge_weight[data.edge_scores < 0.4] = 0.0
            #     node_weight[data.node_scores < 0.4] = 0.0

            loss_dict = {
                'edge_loss': torch.nn.BCELoss(weight=edge_weight)(edge_scores, data.edge_scores),
                'node_loss': torch.nn.BCELoss(weight=node_weight)(node_scores, data.node_scores),
            }

            loss = sum(loss_dict.values())
            loss.backward()

            self.optimizer.step()

            if not self.params.main.disable_wandb:
                wandb.log({"train/loss_total": loss.item(),
                           "train/edge_loss": loss_dict['edge_loss'].item(),
                           "train/node_loss": loss_dict['node_loss'].item()}
                          )

            # # Visualization
            if self.total_step % 500 == 0:
                if self.params.model.dataparallel:
                    data = data_orig
                self.do_logging(data, edge_scores, node_scores, 'train/Images')

            t_end = time.time()
            avg_time_per_sample = (t_end - epoch_start) / (step + 1) / self.params.model.batch_size

            text = 'Epoch {} / {}, it {} / {}, it glob {}, train loss = {:03f} | Batch time  {:.3f} | Avg sample time: {:.3f}'.\
                format(epoch, self.params.model.num_epochs, step+1, len(self.dataloader_train), epoch * len(self.dataloader_train) + step+1, loss.item(), t_end-t_start, avg_time_per_sample)
            train_progress.set_description(text)

            self.total_step += 1

        if not self.params.main.disable_wandb:
            wandb.log({"train/epoch": epoch})


    def eval(self):

        self.model.eval()
        print('Evaluating...')

        random_index = np.random.randint(0, len(self.dataloader_val))

        dataloader_progress = tqdm(self.dataloader_val, desc='Evaluating...')

        node_losses = []
        edge_losses = []
        acc_edge_list = []
        acc_node_list = []
        precision_edge_list = []
        precision_node_list = []
        recall_edge_list = []
        recall_node_list = []
        metrics_dict_list = []

        for i_val, data in enumerate(dataloader_progress):

            if self.params.model.dataparallel:
                data = [item.to(self.device) for item in data]
            else:
                data = data.to(self.device)

            with torch.no_grad():
                edge_scores, node_scores, _ = self.model(data)
                edge_scores = torch.nn.Sigmoid()(edge_scores).squeeze()
                node_scores = torch.nn.Sigmoid()(node_scores).squeeze()

            # Convert list of Data to DataBatch for post-processing and loss calculation
            if self.params.model.dataparallel:
                data_orig = data.copy()
                data = Batch.from_data_list(data)

            # loss and optim
            edge_weight = torch.ones_like(data.edge_scores)
            node_weight = torch.ones_like(data.node_scores)

            try:
                edge_loss = torch.nn.BCELoss(weight=edge_weight)(edge_scores, data.edge_scores)
                node_loss = torch.nn.BCELoss(weight=node_weight)(node_scores, data.node_scores)
            except Exception as e:
                print(e)
                continue

            node_losses.append(node_loss.item())
            edge_losses.append(edge_loss.item())

            node_scores = node_scores.cpu()
            edge_scores = edge_scores.cpu()
            data = data.cpu()

            acc_node = Accuracy(num_classes=1)(node_scores, torch.round(data.node_scores).int())
            acc_edge = Accuracy(num_classes=1)(edge_scores, torch.round(data.edge_scores).int())
            recall_node = Recall(task="binary")(torch.round(node_scores), torch.round(data.node_scores).int())
            recall_edge = Recall(task="binary")(torch.round(edge_scores), torch.round(data.edge_scores).int())
            precision_node = Precision(task="binary")(torch.round(node_scores),
                                                      torch.round(data.node_scores).int())
            precision_edge = Precision(task="binary")(torch.round(edge_scores),
                                                      torch.round(data.edge_scores).int())

            recall_edge_list.append(recall_edge.item())
            recall_node_list.append(recall_node.item())
            precision_edge_list.append(precision_edge.item())
            precision_node_list.append(precision_node.item())
            acc_node_list.append(acc_node.item())
            acc_edge_list.append(acc_edge.item())

            # Visualization
            if i_val == random_index:
                if self.params.model.dataparallel:
                    data = data_orig
                self.do_logging(data, edge_scores, node_scores, plot_text='test/Images')

        re = np.mean(recall_edge_list)
        rn = np.mean(recall_node_list)
        pe = np.mean(precision_edge_list)
        pn = np.mean(precision_node_list)
        ae = np.mean(acc_edge_list)
        an = np.mean(acc_node_list)
        nl = np.mean(node_losses)
        el = np.mean(edge_losses)

        # Calculate mean values for all metrics in metrics_dict
        # metrics_dict_mean = {}
        # for key in metrics_dict_list[0].keys():
        #     metrics_dict_mean[key] = np.mean([metrics_dict[key] for metrics_dict in metrics_dict_list])
        #     print('{}: {:.3f}'.format(key, metrics_dict_mean[key])

        if not self.params.main.disable_wandb:
            wandb.log({"test/loss_total": nl + el,
                       "test/edge_loss": el,
                       "test/node_loss": nl,
                       "test/acc_edge": ae,
                       "test/acc_node": an,
                       "test/recall_edge": re,
                       "test/recall_node": rn,
                       "test/precision_edge": pe,
                       "test/precision_node": pn}
                      )

def main():

    # ----------- Parameter sourcing --------------

    parser = argparse.ArgumentParser(description="Train LaneMP architecture")

    # General parameters (namespace: main)
    parser.add_argument('--config', type=str, help='Provide a config YAML!', required=True)
    parser.add_argument('--dataset', type=str, help="dataset path")
    parser.add_argument('--version', type=str, help="define the dataset version that is used")

    opt = parser.parse_args()

    params = ParamLib(opt.config)
    params.main.overwrite(opt)
    params.preprocessing.overwrite(opt)
    params.model.overwrite(opt)

    print("Batch size summed over all GPUs: ", params.model.batch_size)
    
    if not params.main.disable_wandb:
        wandb.login()
        wandb.init(
            entity='jannik-zuern',
            project='autograph-lanegnn',
            notes='v0.1',
            settings=wandb.Settings(start_method="fork"),
        )
        wandb.config.update(params.paths)
        wandb.config.update(params.model)
        wandb.config.update(params.preprocessing)


    # -------  Model, optimizer and data initialization ------

    model = LaneGNN(gnn_depth=params.model.gnn_depth,
                    edge_geo_dim=params.model.edge_geo_dim,
                    map_feat_dim=params.model.map_feat_dim,
                    edge_dim=params.model.edge_dim,
                    node_dim=params.model.node_dim,
                    msg_dim=params.model.msg_dim,
                    in_channels=params.model.in_channels,
                    )


    model = model.to(params.model.device)

    # Make model parallel if available
    if params.model.dataparallel:
        print("Let's use DataParallel with", torch.cuda.device_count(), "GPUs!")
        model = DataParallel(model)
    else:
        print("Let's NOT use DataParallel with", torch.cuda.device_count(), "GPUs!")

    # Load model weights
    #model_path = '/home/zuern/self-supervised-graph/checkpoints/lanemp_local_run_0800.pth'
    # model.load_state_dict(torch.load(model_path))
    #print('Model loaded from {}'.format(model_path))


    weights = [w for w in model.parameters() if w.requires_grad]

    optimizer = torch.optim.Adam(weights,
                                 lr=float(params.model.lr),
                                 weight_decay=float(params.model.weight_decay),
                                 betas=(params.model.beta_lo, params.model.beta_hi))

    train_path = os.path.join(params.paths.dataroot, params.paths.config_name, 'train')
    val_path = os.path.join(params.paths.dataroot, params.paths.config_name, 'val')
    dataset_train = PreprocessedDataset(path=train_path)
    dataset_val = PreprocessedDataset(path=val_path)

    if params.model.dataparallel:
        dataloader_obj = DataListLoader
    else:
        dataloader_obj = torch_geometric.loader.DataLoader

    dataloader_train = dataloader_obj(dataset_train,
                                      batch_size=params.model.batch_size,
                                      num_workers=params.model.loader_workers,
                                      shuffle=True)
    dataloader_val = dataloader_obj(dataset_val,
                                     batch_size=1,
                                     num_workers=1,
                                     shuffle=False)

    trainer = Trainer(params, model, dataloader_train, dataloader_val, optimizer)

    for epoch in range(params.model.num_epochs):
        trainer.train(epoch)

        #if not params.main.disable_wandb:
        if epoch % 100 == 0:
            try:
                wandb_run_name = wandb.run.name
            except:
                wandb_run_name = "local_run"

            fname = 'lanegnn_{}_{:04d}.pth'.format(wandb_run_name, epoch)
            checkpoint_path = os.path.join(params.paths.checkpoints, fname)
            print("Saving checkpoint to {}".format(checkpoint_path))

            torch.save(model.state_dict(), checkpoint_path)

        # Evaluate
        trainer.eval()


if __name__ == '__main__':
    main()
